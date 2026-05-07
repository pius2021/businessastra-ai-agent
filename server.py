"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import ssl
import certifi
import aiohttp
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Dict, Set

# Patch SSL with certifi before any network operations
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from db import (
    SENSITIVE_KEYS, cancel_appointment, clear_errors, create_campaign, delete_campaign,
    get_all_appointments, get_all_calls, get_all_campaigns, get_all_settings,
    get_all_agent_profiles, get_agent_profile, create_agent_profile, update_agent_profile,
    delete_agent_profile, set_default_agent_profile, get_calls_by_phone, get_campaign,
    get_contacts, get_errors, get_logs, get_setting, get_stats, log_error,
    save_settings, set_setting, update_call_notes, update_campaign_run_stats,
    update_campaign_status, init_pool, close_pool, ensure_tables,
    create_uploaded_list, get_uploaded_list, get_uploaded_list_row,
    get_uploaded_list_rows, get_uploaded_lists, update_uploaded_list_mapping,
    update_uploaded_list_row_call, create_user, get_user_by_username, get_user_by_id,
    get_all_users, update_user_active_status, log_call,
    save_conversation_event, get_conversation_history
)
from prompts import DEFAULT_SYSTEM_PROMPT
from config import SARVAM_VOICE_MODELS, SARVAM_LANGUAGE_CODES

load_dotenv(".env", override=True)

from logger import setup_logging
setup_logging()

logger = logging.getLogger("server")

# ── Authentication Setup ────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 24 * 60  # 24 hours

# Simple password hashing using hashlib + salt (avoiding bcrypt issues)
import hashlib
import secrets as secrets_module

def hash_password(password: str) -> str:
    """Hash a password using SHA256 with salt."""
    salt = secrets_module.token_hex(32)
    pwd_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${pwd_hash}"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash."""
    try:
        salt, pwd_hash = hashed_password.split('$')
        return hashlib.sha256((salt + plain_password).encode()).hexdigest() == pwd_hash
    except:
        return False

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# ── Pydantic Models ────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    full_name: str
    user_type: str = "normal_user"

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user_type: str
    username: str

class UserInfo(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    user_type: str
    is_active: bool

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed — campaign scheduling disabled")

# ── WebSocket Connection Manager ────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.call_subscribers: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        if client_id not in self.active_connections:
            self.active_connections[client_id] = set()
        self.active_connections[client_id].add(websocket)

    def disconnect(self, websocket: WebSocket, client_id: str):
        if client_id in self.active_connections:
            self.active_connections[client_id].discard(websocket)
            if not self.active_connections[client_id]:
                del self.active_connections[client_id]
        
        # Remove from call subscriptions
        for room_name in self.call_subscribers:
            if websocket in self.call_subscribers[room_name]:
                self.call_subscribers[room_name].remove(websocket)

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_text(json.dumps(message))

    async def broadcast_to_call(self, message: dict, room_name: str):
        if room_name in self.call_subscribers:
            disconnected = []
            for connection in self.call_subscribers[room_name].copy():
                try:
                    await connection.send_text(json.dumps(message))
                except:
                    disconnected.append(connection)
            
            # Remove disconnected connections
            for conn in disconnected:
                self.call_subscribers[room_name].discard(conn)

    async def subscribe_to_call(self, websocket: WebSocket, room_name: str):
        if room_name not in self.call_subscribers:
            self.call_subscribers[room_name] = set()
        self.call_subscribers[room_name].add(websocket)

    def unsubscribe_from_call(self, websocket: WebSocket, room_name: str):
        if room_name in self.call_subscribers:
            self.call_subscribers[room_name].discard(websocket)
            if not self.call_subscribers[room_name]:
                del self.call_subscribers[room_name]

manager = ConnectionManager()

app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files from ui/ directory
app.mount("/ui", StaticFiles(directory="ui"), name="ui")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    logger.info(
        f"{request.method} {request.url.path} - "
        f"Status: {response.status_code} - "
        f"Duration: {process_time:.3f}s"
    )
    return response

# ── Startup / Shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    pool = await init_pool()
    await ensure_tables(pool)
    if _scheduler:
        _scheduler.start()
        await _reschedule_all_campaigns()

@app.on_event("shutdown")
async def _shutdown():
    await close_pool()
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)

# ── Helpers ────────────────────────────────────────────────────────────────────

async def eff(key: str) -> str:
    """Effective setting: DB value takes priority over env var."""
    val = await get_setting(key, "")
    return val if val else os.getenv(key, "")


def _column_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _pick_column(columns: list, candidates: list) -> Optional[str]:
    keyed = {_column_key(col): col for col in columns}
    for candidate in candidates:
        found = keyed.get(_column_key(candidate))
        if found:
            return found
    for col in columns:
        key = _column_key(col)
        if any(_column_key(candidate) in key for candidate in candidates):
            return col
    return None


def _normalize_phone(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.endswith(".0"):
        raw = raw[:-2]
    digits = re.sub(r"\D+", "", raw)
    if raw.startswith("+") and digits:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if len(digits) > 10:
        return f"+{digits}"
    return raw


def _room_created_at_iso(value) -> Optional[str]:
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp).isoformat()
    return str(value)


def _row_context(row_data: dict) -> str:
    parts = []
    for key, value in row_data.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}: {value}")
    return "\n".join(parts)


def _parse_upload_bytes(filename: str, file_base64: str) -> tuple[list, list]:
    try:
        if "," in file_base64 and file_base64.split(",", 1)[0].startswith("data:"):
            file_base64 = file_base64.split(",", 1)[1]
        raw = base64.b64decode(file_base64)
    except Exception as exc:
        raise HTTPException(400, f"Invalid file payload: {exc}")

    try:
        import pandas as pd
        suffix = Path(filename).suffix.lower()
        if suffix == ".csv":
            df = pd.read_csv(io.BytesIO(raw), dtype=object)
        elif suffix in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(raw), dtype=object)
        else:
            raise HTTPException(400, "Upload a .xlsx, .xls, or .csv file")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Could not read spreadsheet: {exc}")

    df = df.dropna(how="all")
    original_columns = list(df.columns)
    columns = [str(col).strip() for col in original_columns if str(col).strip()]
    if not columns:
        raise HTTPException(400, "Spreadsheet has no header columns")
    rename_map = {old: str(old).strip() for old in original_columns}
    df = df.rename(columns=rename_map)

    rows = []
    for _, record in df.iterrows():
        row = {}
        for col in columns:
            value = record.get(col)
            if value is None:
                row[col] = ""
                continue
            try:
                import pandas as pd
                if pd.isna(value):
                    row[col] = ""
                    continue
            except Exception:
                pass
            row[col] = str(value).strip()
        if any(str(v).strip() for v in row.values()):
            rows.append(row)
    if not rows:
        raise HTTPException(400, "Spreadsheet has headers but no data rows")
    return columns, rows

# ── Request models ────────────────────────────────────────────────────────────

class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    customer_context: str = ""
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None

class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "shubh"
    voice_model: str = "bulbul:v3"
    language_code: str = "od-IN"
    model: str = "sarvam-30b"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False

class PromptRequest(BaseModel):
    prompt: str

class SettingsRequest(BaseModel):
    settings: dict

class NotesRequest(BaseModel):
    notes: str

class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None

class StatusRequest(BaseModel):
    status: str

class UploadedListRequest(BaseModel):
    name: Optional[str] = None
    filename: str
    file_base64: str
    phone_column: Optional[str] = None
    lead_name_column: Optional[str] = None

class UploadedListMappingRequest(BaseModel):
    phone_column: Optional[str] = None
    lead_name_column: Optional[str] = None

class UploadedRowCallRequest(BaseModel):
    agent_profile_id: Optional[str] = None
    system_prompt: Optional[str] = None

# ── Authentication Endpoints ────────────────────────────────────────────────────

@app.post("/register")
async def register(req: RegisterRequest):
    """Register a new user."""
    try:
        # Check if user already exists
        existing_user = await get_user_by_username(req.username)
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already taken")
        
        # Hash password and create user
        password_hash = hash_password(req.password)
        user = await create_user(
            username=req.username,
            email=req.email,
            password_hash=password_hash,
            full_name=req.full_name,
            user_type=req.user_type
        )
        
        if not user:
            raise HTTPException(status_code=400, detail="Failed to create user")
        
        return {"message": "User registered successfully", "username": req.username}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(status_code=500, detail="Registration failed")


@app.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """Login user and return JWT token."""
    try:
        # Get user by username
        user = await get_user_by_username(req.username)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Verify password
        if not verify_password(req.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Create JWT token
        access_token = create_access_token({"sub": req.username, "user_type": user["user_type"]})
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user_type": user["user_type"],
            "username": user["username"]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Login failed")


@app.get("/user/me", response_model=UserInfo)
async def get_current_user(request: Request):
    """Get current user info from JWT token."""
    try:
        # Get token from Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid token")
        
        token = auth_header[7:]  # Remove "Bearer " prefix
        
        # Decode JWT token
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username = payload.get("sub")
            if not username:
                raise HTTPException(status_code=401, detail="Invalid token")
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # Get user from database
        user = await get_user_by_username(username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "full_name": user["full_name"],
            "user_type": user["user_type"],
            "is_active": bool(user["is_active"])
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get user error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user")


@app.post("/logout")
async def logout():
    """Logout endpoint (client-side token deletion)."""
    return {"message": "Logged out successfully"}

# ── WebSocket for Real-time Call Monitoring ───────────────────────────────────────

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket, client_id)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("type") == "subscribe_call":
                room_name = message.get("room_name")
                if room_name:
                    await manager.subscribe_to_call(websocket, room_name)
                    await manager.send_personal_message(
                        {"type": "subscribed", "room_name": room_name}, 
                        websocket
                    )
            elif message.get("type") == "unsubscribe_call":
                room_name = message.get("room_name")
                if room_name:
                    manager.unsubscribe_from_call(websocket, room_name)
                    await manager.send_personal_message(
                        {"type": "unsubscribed", "room_name": room_name}, 
                        websocket
                    )
                    
    except WebSocketDisconnect:
        manager.disconnect(websocket, client_id)

# ── Call Monitoring Endpoints ─────────────────────────────────────────────────────

@app.post("/api/call-monitor/{room_name}/event")
async def send_call_event(room_name: str, request: Request):
    """Send real-time call event to subscribed clients"""
    try:
        event = await request.json()
    except Exception:
        event = {}

    try:
        await save_conversation_event(
            room_name=room_name,
            speaker=event.get("speaker", "unknown"),
            text=event.get("text", ""),
            timestamp=event.get("timestamp", datetime.utcnow().isoformat())
        )
    except Exception as e:
        logger.error(f"Failed to save conversation event: {e}")

    await manager.broadcast_to_call({
        "type": "call_event",
        "room_name": room_name,
        "timestamp": datetime.utcnow().isoformat(),
        "data": event
    }, room_name)
    return {"status": "sent"}

@app.get("/api/call-monitor/{room_name}/history")
async def get_call_history(room_name: str):
    """Get the full conversation history for a call room."""
    try:
        history = await get_conversation_history(room_name)
        return {"room_name": room_name, "history": history}
    except Exception as e:
        logger.error(f"Failed to fetch conversation history: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch conversation history: {e}")

@app.post("/api/call-monitor/{room_name}/participant-disconnect")
async def notify_participant_disconnect(room_name: str, request: Request):
    """Notify that a participant has disconnected"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    logger.info("Participant disconnect notification: room=%s phone=%s", room_name, data.get("phone_number"))
    await manager.broadcast_to_call({
        "type": "participant_disconnected",
        "room_name": room_name,
        "phone_number": data.get("phone_number"),
        "timestamp": datetime.utcnow().isoformat(),
        "message": f"Participant {data.get('phone_number')} disconnected"
    }, room_name)
    return {"status": "notified"}

@app.post("/api/call/{room_name}/end")
async def end_call(room_name: str):
    """End a call and notify subscribers"""
    session = None
    lk = None
    try:
        from livekit import api as lk_api
        url = await eff("LIVEKIT_URL")
        key = await eff("LIVEKIT_API_KEY")
        secret = await eff("LIVEKIT_API_SECRET")
        
        if not all([url, key, secret]):
            raise HTTPException(400, "LiveKit credentials not configured")
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        # Delete the room to end the call
        await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
        
        # Notify subscribers
        await manager.broadcast_to_call({
            "type": "call_ended",
            "room_name": room_name,
            "timestamp": datetime.utcnow().isoformat(),
            "message": "Call ended by operator"
        }, room_name)
        
        logger.info(f"Call ended: room={room_name}")
        return {"status": "ended", "room_name": room_name}
        
    except Exception as exc:
        logger.error(f"Failed to end call {room_name}: {exc}")
        raise HTTPException(500, f"Failed to end call: {exc}")
    finally:
        if lk:
            await lk.aclose()
        if session and not session.closed:
            await session.close()

@app.get("/api/call/{room_name}/status")
async def get_call_status(room_name: str):
    """Get current status of a call room"""
    session = None
    lk = None
    try:
        from livekit import api as lk_api
        url = await eff("LIVEKIT_URL")
        key = await eff("LIVEKIT_API_KEY")
        secret = await eff("LIVEKIT_API_SECRET")
        
        if not all([url, key, secret]):
            raise HTTPException(400, "LiveKit credentials not configured")
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        # Get room info
        room_list = await lk.room.list_rooms(lk_api.ListRoomsRequest())
        room_info = next((room for room in room_list.rooms if room.name == room_name), None)
        
        if room_info:
            return {
                "room_name": room_name,
                "active": True,
                "participants": room_info.num_participants,
                "created_at": _room_created_at_iso(room_info.creation_time)
            }
        else:
            return {"room_name": room_name, "active": False}
            
    except Exception as exc:
        logger.error(f"Failed to get call status {room_name}: {exc}")
        return {"room_name": room_name, "active": False, "error": str(exc)}
    finally:
        if lk:
            await lk.aclose()
        if session and not session.closed:
            await session.close()

# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        from db import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as exc:
        return JSONResponse({"status": "degraded", "db": str(exc)}, status_code=503)

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404)

# ── Call dispatch ─────────────────────────────────────────────────────────────

@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")

    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured. Go to Settings → LiveKit.")

    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must be in E.164 format: +919876543210")

    effective_prompt = req.system_prompt
    effective_voice = None
    effective_voice_model = None
    effective_language_code = None
    effective_model = None
    effective_tools = None

    if req.agent_profile_id:
        profile = await get_agent_profile(req.agent_profile_id)
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_voice_model = profile.get("voice_model")
            effective_language_code = profile.get("language_code")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")

    if not effective_prompt:
        effective_prompt = await get_setting("system_prompt", "") or None

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata: dict = {
        "phone_number": phone,
        "lead_name": req.lead_name,
        "customer_context": req.customer_context,
        "system_prompt": effective_prompt,
    }
    if effective_voice:
        metadata["voice_override"] = effective_voice
    if effective_voice_model:
        metadata["voice_model_override"] = effective_voice_model
    if effective_language_code:
        metadata["language_code"] = effective_language_code
    if effective_model:
        metadata["model_override"] = effective_model
    if effective_tools:
        metadata["tools_override"] = effective_tools

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)
            )
        )
        await lk.aclose()
        await session.close()
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name}", "info")
        # Log the call dispatch
        await log_call(
            phone_number=phone,
            lead_name=req.lead_name,
            outcome="dispatched",
            reason="",
            duration_seconds=0,
            room_name=room_name,
        )
        return {"status": "dispatched", "room": room_name, "phone": phone}
    except Exception as exc:
        logger.error("Dispatch error: %s", exc, exc_info=True)
        raise HTTPException(500, f"Dispatch failed: {exc}")

# ── Calls ─────────────────────────────────────────────────────────────────────

@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)

@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}

# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()

# ── Appointments ──────────────────────────────────────────────────────────────

@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)

@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}

# ── Prompt ────────────────────────────────────────────────────────────────────

@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}

@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    await set_setting("system_prompt", req.prompt)
    return {"status": "saved"}

@app.delete("/api/prompt")
async def api_reset_prompt():
    await set_setting("system_prompt", "")
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}

# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()

@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    await save_settings(filtered)
    for k, v in filtered.items():
        os.environ[k] = str(v)
    return {"status": "saved", "count": len(filtered)}

# ── SIP trunk setup ───────────────────────────────────────────────────────────

@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url        = await eff("LIVEKIT_URL")
    key        = await eff("LIVEKIT_API_KEY")
    secret     = await eff("LIVEKIT_API_SECRET")
    sip_domain = await eff("VOBIZ_SIP_DOMAIN")
    username   = await eff("VOBIZ_USERNAME")
    password   = await eff("VOBIZ_PASSWORD")
    phone      = await eff("VOBIZ_OUTBOUND_NUMBER")

    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, "Configure LiveKit and Vobiz credentials in Settings first.")

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=sip_domain,
                    auth_username=username,
                    auth_password=password,
                    numbers=[phone],
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        await lk.aclose()
        await session.close()

        # LiveKit SIP trunk IDs must start with ST_
        if not trunk_id or not trunk_id.startswith("ST_"):
            logger.error("Trunk creation returned invalid ID: %r", trunk_id)
            raise HTTPException(500, f"Trunk created but returned invalid ID: {trunk_id!r}. Try again.")

        logger.info("SIP trunk created: %s (address=%s, number=%s)", trunk_id, sip_domain, phone)
        await set_setting("OUTBOUND_TRUNK_ID", trunk_id)
        os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        return {"status": "created", "trunk_id": trunk_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")

# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)

@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}

# ── CRM ───────────────────────────────────────────────────────────────────────

@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}

@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}

@app.post("/api/uploaded-lists")
async def api_upload_list(req: UploadedListRequest):
    columns, raw_rows = _parse_upload_bytes(req.filename, req.file_base64)
    phone_column = req.phone_column or _pick_column(
        columns,
        ["phone", "phone number", "mobile", "mobile number", "customer number", "contact number", "number"],
    )
    lead_name_column = req.lead_name_column or _pick_column(
        columns,
        ["lead_name", "lead name", "name", "customer name", "customer", "full name"],
    )
    if phone_column and phone_column not in columns:
        raise HTTPException(400, f"Phone column '{phone_column}' does not exist in this sheet")
    if lead_name_column and lead_name_column not in columns:
        raise HTTPException(400, f"Lead name column '{lead_name_column}' does not exist in this sheet")

    rows = []
    for row in raw_rows:
        rows.append({
            "data": row,
            "phone_number": _normalize_phone(row.get(phone_column)) if phone_column else "",
            "lead_name": str(row.get(lead_name_column) or "").strip() if lead_name_column else "",
        })

    list_id = await create_uploaded_list(
        name=(req.name or Path(req.filename).stem or "Uploaded List").strip(),
        source_filename=req.filename,
        columns=columns,
        rows=rows,
        phone_column=phone_column,
        lead_name_column=lead_name_column,
    )
    return {"status": "uploaded", "list": await get_uploaded_list(list_id)}

@app.get("/api/uploaded-lists")
async def api_list_uploaded_lists():
    return await get_uploaded_lists()

@app.get("/api/uploaded-lists/{list_id}")
async def api_get_uploaded_list(list_id: str):
    uploaded = await get_uploaded_list(list_id)
    if not uploaded:
        raise HTTPException(404, "Uploaded list not found")
    return uploaded

@app.patch("/api/uploaded-lists/{list_id}/mapping")
async def api_update_uploaded_mapping(list_id: str, req: UploadedListMappingRequest):
    uploaded = await get_uploaded_list(list_id)
    if not uploaded:
        raise HTTPException(404, "Uploaded list not found")
    columns = uploaded.get("columns") or []
    if req.phone_column and req.phone_column not in columns:
        raise HTTPException(400, "Phone column does not exist in this list")
    if req.lead_name_column and req.lead_name_column not in columns:
        raise HTTPException(400, "Lead name column does not exist in this list")
    ok = await update_uploaded_list_mapping(list_id, req.phone_column, req.lead_name_column)
    if not ok:
        raise HTTPException(404, "Uploaded list not found")
    return await get_uploaded_list(list_id)

@app.get("/api/uploaded-lists/{list_id}/rows")
async def api_get_uploaded_rows(list_id: str, limit: int = 500, offset: int = 0):
    uploaded = await get_uploaded_list(list_id)
    if not uploaded:
        raise HTTPException(404, "Uploaded list not found")
    rows = await get_uploaded_list_rows(list_id, limit=limit, offset=offset)
    return {"list": uploaded, "rows": rows}

@app.post("/api/uploaded-list-rows/{row_id}/call")
async def api_call_uploaded_row(row_id: str, req: UploadedRowCallRequest):
    row = await get_uploaded_list_row(row_id)
    if not row:
        raise HTTPException(404, "Uploaded row not found")
    uploaded = await get_uploaded_list(row["list_id"])
    if not uploaded:
        raise HTTPException(404, "Uploaded list not found")

    row_data = row.get("data") or {}
    phone_column = uploaded.get("phone_column")
    lead_name_column = uploaded.get("lead_name_column")
    phone = _normalize_phone(row_data.get(phone_column) if phone_column else row.get("phone_number"))
    lead_name = str(row_data.get(lead_name_column) or row.get("lead_name") or "there").strip() or "there"
    context = _row_context(row_data)

    if not phone or not phone.startswith("+"):
        await update_uploaded_list_row_call(row_id, "failed", call_error="No valid E.164 phone number found")
        raise HTTPException(400, "No valid phone number found. Set the phone column or use E.164 format.")

    try:
        await update_uploaded_list_row_call(row_id, "in_progress")
        result = await api_dispatch_call(CallRequest(
            phone=phone,
            lead_name=lead_name,
            customer_context=context,
            system_prompt=req.system_prompt,
            agent_profile_id=req.agent_profile_id,
        ))
        await update_uploaded_list_row_call(row_id, "dispatched", room_name=result.get("room"))
        return {"status": "dispatched", "row_id": row_id, "phone": phone, "room": result.get("room")}
    except HTTPException as exc:
        await update_uploaded_list_row_call(row_id, "failed", call_error=str(exc.detail))
        raise
    except Exception as exc:
        await update_uploaded_list_row_call(row_id, "failed", call_error=str(exc))
        raise

# ── Voice Configuration ────────────────────────────────────────────────────────

@app.get("/api/voice-config")
async def api_get_voice_config():
    """Return available voice models, voices per model, and language codes."""
    return {
        "voice_models": SARVAM_VOICE_MODELS,
        "language_codes": [{"code": code, "label": label} for code, label in SARVAM_LANGUAGE_CODES],
    }

# ── Agent Profiles ────────────────────────────────────────────────────────────

@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    try:
        return await get_all_agent_profiles()
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    try:
        profile_id = await create_agent_profile(
            name=req.name, voice=req.voice, voice_model=req.voice_model, 
            language_code=req.language_code, model=req.model,
            system_prompt=req.system_prompt, enabled_tools=req.enabled_tools,
            is_default=req.is_default,
        )
        return {"status": "created", "id": profile_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile

@app.patch("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {
        "name": req.name, "voice": req.voice, "voice_model": req.voice_model,
        "language_code": req.language_code, "model": req.model,
        "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools,
        "is_default": 1 if req.is_default else 0,
    })
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}

@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}

@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    try:
        await set_default_agent_profile(profile_id)
        return {"status": "default set"}
    except Exception as exc:
        raise HTTPException(500, str(exc))

# ── Campaigns ─────────────────────────────────────────────────────────────────

async def _dispatch_one(lk, lk_api, contact: dict, room_name: str,
                        prompt: Optional[str], profile: Optional[dict] = None) -> bool:
    try:
        saved_prompt = prompt or (await get_setting("system_prompt", "")) or None
        metadata: dict = {
            "phone_number": contact.get("phone", ""),
            "lead_name": contact.get("lead_name", "there"),
            "customer_context": contact.get("customer_context", ""),
            "system_prompt": saved_prompt,
        }
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):
                metadata["voice_override"] = profile["voice"]
            if profile.get("voice_model"):
                metadata["voice_model_override"] = profile["voice_model"]
            if profile.get("language_code"):
                metadata["language_code"] = profile["language_code"]
            if profile.get("model"):
                metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"):
                metadata["tools_override"] = profile["enabled_tools"]
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)
            )
        )
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc, exc_info=True)
        return False

async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    delay    = int(campaign.get("call_delay_seconds") or 3)
    prompt   = campaign.get("system_prompt")
    agent_profile_id = campaign.get("agent_profile_id")
    profile = None
    if agent_profile_id:
        profile = await get_agent_profile(agent_profile_id)

    url    = await eff("LIVEKIT_URL")
    key    = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return

    from livekit import api as lk_api_module
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))

    ok_count = fail_count = 0
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for i, contact in enumerate(contacts):
            phone = contact.get("phone", "")
            if not phone.startswith("+"):
                fail_count += 1
                continue
            room_name = f"camp-{campaign_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            success = await _dispatch_one(lk, lk_api_module, contact, room_name, prompt, profile)
            if success:
                ok_count += 1
            else:
                fail_count += 1
            if i < len(contacts) - 1:
                await asyncio.sleep(delay)
        await lk.aclose()
    except Exception as exc:
        logger.error("Campaign run error: %s", exc, exc_info=True)
    finally:
        await session.close()

    await update_campaign_run_stats(campaign_id, ok_count, fail_count)
    logger.info("Campaign %s done — %d dispatched, %d failed", campaign_id, ok_count, fail_count)

async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)

def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", campaign_id, schedule_type, hour, minute)

@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")

    campaign_id = await create_campaign(
        name=req.name, contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type, schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds, system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    campaign = await get_campaign(campaign_id)

    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)

    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}

@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()

@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}

@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}

@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}

# ── Legacy app.py compatibility — forward legacy endpoints ────────────────────
# Keep /api/call-log alive so the old Odia agent.py can still POST to it.

from fastapi import Request as _Req

@app.post("/api/call-log")
async def legacy_call_log(request: _Req):
    """Legacy endpoint used by OdiaVoiceAgent. Stores to call_logs."""
    try:
        payload = await request.json()
        from db import log_call
        await log_call(
            phone_number=payload.get("phone_number", "unknown"),
            lead_name=payload.get("lead_name", payload.get("customer_name", "unknown")),
            outcome=payload.get("outcome", "unknown"),
            reason="",
            duration_seconds=int(payload.get("duration_seconds") or 0),
            room_name=payload.get("room_name"),
        )
        return {"message": "logged"}
    except Exception as exc:
        logger.warning("legacy call-log error: %s", exc)
        return {"message": "logged (best-effort)"}
        
# end point to return list of users from users table. return id and username, username, email, full_name, user_type, is_active
@app.get("/api/users")
async def api_list_users():
    try:
        from db import get_all_users
        users = await get_all_users()
        return {"users": users}
    except Exception as exc:
        logger.error(f"Error fetching users: {exc}", exc_info=True)
        raise HTTPException(500, "Failed to fetch users")
    
# end point to delete a user by id
@app.delete("/api/users/{user_id}")
async def api_delete_user(user_id: int):
    try:
        from db import delete_user
        ok = await delete_user(user_id)
        if not ok:
            raise HTTPException(404, "User not found")
        return {"message": "User deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error deleting user {user_id}: {exc}", exc_info=True)
        raise HTTPException(500, "Failed to delete user")
    
# end point to update a user's is_active status by id
@app.patch("/api/users/{user_id}/status")
async def api_update_user_status(user_id: int, req: StatusRequest):
    try:
        from db import update_user_active_status
        if req.status not in ("active", "inactive"):
            raise HTTPException(400, "status must be: active | inactive")
        is_active = 1 if req.status == "active" else 0
        ok = await update_user_active_status(user_id, is_active)
        if not ok:
            raise HTTPException(404, "User not found")
        return {"message": f"User status updated to {req.status}"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error updating user {user_id} status: {exc}", exc_info=True)
        raise HTTPException(500, "Failed to update user status")    
