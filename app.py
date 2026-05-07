# app.py — FastAPI backend (optimized)
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiomysql
import io
import pandas as pd
import secrets

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, BackgroundTasks, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from livekit import api
from livekit.api import CreateRoomRequest, CreateAgentDispatchRequest
from pydantic import BaseModel

# Authentication imports
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta

from config import (
    ALLOWED_ORIGINS,
    DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME,
    LIVEKIT_AGENT_NAME, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
    clean_display_name, build_token, ensure_env,
)
from logger import setup_logging

setup_logging()
logger = logging.getLogger("voice-agent-app")

ensure_env("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# Authentication Setup
# ---------------------------------------------------------------------------

SECRET_KEY = "your-secret-key-change-in-production-env"  # TODO: Move to .env
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash."""
    return pwd_context.verify(plain_password, hashed_password)

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

# ---------------------------------------------------------------------------
# Pydantic Models — Authentication
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    full_name: str
    user_type: str = "normal_user"  # super_admin, admin, normal_user

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

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Odia Voice Agent", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import time

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

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

REQUIRED_COLUMNS = [
    "Customer Name", "Customer Number", "Loan Amount",
    "Total Installment", "Cost per Installment", "No of Installment Paid",
    "Last Installment Paid on", "Installment Left", "Amount to be Paid",
    "Install Due Date", "Fine for Late Dues",
]

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SessionRequest(BaseModel):
    name: str


class CallLogRequest(BaseModel):
    room_name: str
    customer_name: str
    outcome: str                    # e.g. "promise_to_pay", "not_reachable", "refused", "completed"
    duration_seconds: Optional[int] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event() -> None:
    app.state.pool = None
    try:
        app.state.pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASS,
            db=DB_NAME,
            autocommit=True,
            minsize=2,
            maxsize=20,
            charset="utf8mb4",
        )
        logger.info("Database connection pool created (minsize=2, maxsize=20)")
        await _init_tables(app.state.pool)
    except Exception as e:
        logger.error("Failed to initialize database: %s", e, exc_info=True)


async def _init_tables(pool) -> None:
    """Create all required tables if they do not exist, and migrate existing ones."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Users table (authentication)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(100) NOT NULL UNIQUE,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    full_name VARCHAR(255) NOT NULL,
                    user_type ENUM('super_admin', 'admin', 'normal_user') DEFAULT 'normal_user',
                    is_active TINYINT DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_username (username),
                    INDEX idx_email (email)
                ) CHARACTER SET utf8mb4
            """)

            # Customers table (fresh install)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id              INT AUTO_INCREMENT PRIMARY KEY,
                    customer_name   VARCHAR(255) NOT NULL UNIQUE,
                    customer_number VARCHAR(50)  NOT NULL,
                    loan_amount     VARCHAR(50),
                    total_installment       VARCHAR(50),
                    cost_per_installment    VARCHAR(50),
                    no_of_installment_paid  VARCHAR(50),
                    last_installment_paid_on VARCHAR(50),
                    installment_left        VARCHAR(50),
                    amount_to_be_paid       VARCHAR(50),
                    install_due_date        VARCHAR(50),
                    fine_for_late_dues      VARCHAR(50),
                    call_status     ENUM('pending','in_progress','completed','failed') DEFAULT 'pending',
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) CHARACTER SET utf8mb4
            """)

            # Migration: add call_status to existing tables that were created before this column existed
            await cur.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'customers'
                  AND COLUMN_NAME  = 'call_status'
            """)
            row = await cur.fetchone()
            if row[0] == 0:
                await cur.execute("""
                    ALTER TABLE customers
                    ADD COLUMN call_status ENUM('pending','in_progress','completed','failed') DEFAULT 'pending'
                """)
                logger.info("Migration applied: added call_status column to customers table")

            # Call logs table (new)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS call_logs (
                    id               INT AUTO_INCREMENT PRIMARY KEY,
                    customer_name    VARCHAR(255) NOT NULL,
                    room_name        VARCHAR(255) NOT NULL,
                    outcome          VARCHAR(50),
                    duration_seconds INT,
                    notes            TEXT,
                    started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_customer (customer_name),
                    INDEX idx_room (room_name)
                ) CHARACTER SET utf8mb4
            """)

    logger.info("Database tables verified / created successfully")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if getattr(app.state, "pool", None):
        app.state.pool.close()
        await app.state.pool.wait_closed()
        logger.info("Database connection pool closed")


# ---------------------------------------------------------------------------
# Helper: pool guard
# ---------------------------------------------------------------------------

def _require_pool():
    pool = getattr(app.state, "pool", None)
    if not pool:
        logger.error("Database pool not available")
        raise HTTPException(503, "Service unavailable: database not initialized")
    return pool


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Readiness probe — checks DB connectivity."""
    pool = getattr(app.state, "pool", None)
    db_ok = False
    if pool:
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    db_ok = True
        except Exception as e:
            logger.warning("Health-check DB ping failed: %s", e)

    status = "ok" if db_ok else "degraded"
    return {"status": status, "db": "connected" if db_ok else "disconnected"}


# ---------------------------------------------------------------------------
# Authentication Endpoints
# ---------------------------------------------------------------------------

@app.post("/register", response_model=dict)
async def register(req: RegisterRequest):
    """Register a new user."""
    pool = _require_pool()
    
    # Validate user_type
    if req.user_type not in ["super_admin", "admin", "normal_user"]:
        raise HTTPException(400, "Invalid user_type. Must be super_admin, admin, or normal_user")
    
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Check if username already exists
                await cur.execute("SELECT id FROM users WHERE username = %s", (req.username,))
                if await cur.fetchone():
                    raise HTTPException(400, "Username already exists")
                
                # Check if email already exists
                await cur.execute("SELECT id FROM users WHERE email = %s", (req.email,))
                if await cur.fetchone():
                    raise HTTPException(400, "Email already exists")
                
                # Hash password and insert user
                password_hash = hash_password(req.password)
                await cur.execute("""
                    INSERT INTO users (username, email, password_hash, full_name, user_type)
                    VALUES (%s, %s, %s, %s, %s)
                """, (req.username, req.email, password_hash, req.full_name, req.user_type))
                
                logger.info(f"New user registered: {req.username} ({req.user_type})")
                return {"message": "User registered successfully", "username": req.username}
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(500, "Registration failed")


@app.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """Login a user and return JWT token."""
    pool = _require_pool()
    
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Fetch user by username
                await cur.execute("""
                    SELECT id, username, password_hash, user_type, is_active
                    FROM users WHERE username = %s
                """, (req.username,))
                user = await cur.fetchone()
                
                if not user:
                    raise HTTPException(401, "Invalid credentials")
                
                user_id, username, password_hash, user_type, is_active = user
                
                # Check if user is active
                if not is_active:
                    raise HTTPException(401, "User account is inactive")
                
                # Verify password
                if not verify_password(req.password, password_hash):
                    raise HTTPException(401, "Invalid credentials")
                
                # Create JWT token
                access_token = create_access_token(
                    data={"sub": username, "user_id": user_id, "user_type": user_type}
                )
                
                logger.info(f"User logged in: {username}")
                return TokenResponse(
                    access_token=access_token,
                    token_type="bearer",
                    user_type=user_type,
                    username=username
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(500, "Login failed")


@app.get("/user/me", response_model=UserInfo)
async def get_current_user(token: str = None, request: Request = None):
    """Get current logged-in user info from JWT token."""
    pool = _require_pool()
    
    # Try to get token from header if not provided
    if not token and request:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
    
    if not token:
        raise HTTPException(401, "Not authenticated")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(401, "Invalid token")
    except JWTError:
        raise HTTPException(401, "Invalid token")
    
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT id, username, email, full_name, user_type, is_active
                    FROM users WHERE username = %s
                """, (username,))
                user = await cur.fetchone()
                
                if not user:
                    raise HTTPException(404, "User not found")
                
                user_id, username, email, full_name, user_type, is_active = user
                return UserInfo(
                    id=user_id,
                    username=username,
                    email=email,
                    full_name=full_name,
                    user_type=user_type,
                    is_active=bool(is_active)
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user info: {e}")
        raise HTTPException(500, "Failed to retrieve user info")


# ---------------------------------------------------------------------------
# Authentication Dependency
# ---------------------------------------------------------------------------

def get_current_active_user(request: Request):
    """Dependency to get current active user from JWT token."""
    pool = _require_pool()
    
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    
    token = auth_header[7:]
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(401, "Invalid token")
    except JWTError:
        raise HTTPException(401, "Invalid token")
    
    try:
        async def _get_user():
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT id, username, email, full_name, user_type, is_active
                        FROM users WHERE username = %s
                    """, (username,))
                    user = await cur.fetchone()
                    
                    if not user:
                        raise HTTPException(404, "User not found")
                    
                    user_id, username, email, full_name, user_type, is_active = user
                    
                    if not is_active:
                        raise HTTPException(400, "Inactive user account")
                    
                    return {
                        "id": user_id,
                        "username": username,
                        "email": email,
                        "full_name": full_name,
                        "user_type": user_type,
                        "is_active": bool(is_active)
                    }
        return _get_user()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting current user: {e}")
        raise HTTPException(500, "Failed to retrieve user info")


@app.post("/logout")
async def logout():
    """Logout (client-side deletes token)."""
    return {"message": "Logged out successfully"}


# ---------------------------------------------------------------------------
# Excel upload
# ---------------------------------------------------------------------------

async def _process_excel_background(df: pd.DataFrame, pool) -> None:
    """Batch-insert / upsert all rows using executemany (10× faster than per-row)."""
    rows = []
    for _, row in df.iterrows():
        rows.append(tuple(
            str(row[col]).strip() if pd.notna(row[col]) else None
            for col in REQUIRED_COLUMNS
        ))

    insert_sql = """
        INSERT INTO customers (
            customer_name, customer_number, loan_amount,
            total_installment, cost_per_installment, no_of_installment_paid,
            last_installment_paid_on, installment_left, amount_to_be_paid,
            install_due_date, fine_for_late_dues
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            customer_number          = VALUES(customer_number),
            loan_amount              = VALUES(loan_amount),
            total_installment        = VALUES(total_installment),
            cost_per_installment     = VALUES(cost_per_installment),
            no_of_installment_paid   = VALUES(no_of_installment_paid),
            last_installment_paid_on = VALUES(last_installment_paid_on),
            installment_left         = VALUES(installment_left),
            amount_to_be_paid        = VALUES(amount_to_be_paid),
            install_due_date         = VALUES(install_due_date),
            fine_for_late_dues       = VALUES(fine_for_late_dues),
            call_status              = call_status   -- preserve existing status
    """
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(insert_sql, rows)
        logger.info("Batch-inserted %d customer rows", len(rows))
    except Exception as e:
        logger.error("Batch insert failed: %s", e, exc_info=True)


@app.post("/upload-excel")
async def upload_excel(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    pool = _require_pool()

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx / .xls files are accepted")

    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        df.columns = df.columns.str.strip()
    except Exception as e:
        raise HTTPException(400, f"Could not parse Excel file: {e}") from e

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise HTTPException(400, f"Missing required columns: {missing}")

    row_count = len(df)
    background_tasks.add_task(_process_excel_background, df, pool)
    return {
        "message": f"File accepted. Processing {row_count} rows in background.",
        "row_count": row_count,
    }


# ---------------------------------------------------------------------------
# Customers — with pagination + server-side search
# ---------------------------------------------------------------------------

@app.get("/customers")
async def get_customers(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=100),
):
    pool = _require_pool()
    offset = (page - 1) * limit
    like = f"%{search}%"

    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                # Total count
                await cur.execute(
                    "SELECT COUNT(*) AS total FROM customers WHERE customer_name LIKE %s OR customer_number LIKE %s",
                    (like, like),
                )
                total = (await cur.fetchone())["total"]

                # Page
                await cur.execute(
                    """SELECT * FROM customers
                       WHERE customer_name LIKE %s OR customer_number LIKE %s
                       ORDER BY created_at DESC
                       LIMIT %s OFFSET %s""",
                    (like, like, limit, offset),
                )
                rows = await cur.fetchall()

        customers = {}
        for r in rows:
            customers[r["customer_name"]] = {
                "Customer Name":              r["customer_name"],
                "Customer Number":            r["customer_number"],
                "Loan Amount":                r["loan_amount"],
                "Total Installment":          r["total_installment"],
                "Cost per Installment":       r["cost_per_installment"],
                "No of Installment Paid":     r["no_of_installment_paid"],
                "Last Installment Paid on":   r["last_installment_paid_on"],
                "Installment Left":           r["installment_left"],
                "Amount to be Paid":          r["amount_to_be_paid"],
                "Install Due Date":           r["install_due_date"],
                "Fine for Late Dues":         r["fine_for_late_dues"],
                "call_status":                r.get("call_status", "pending"),
            }

        return {
            "customers": customers,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, -(-total // limit)),   # ceiling division
        }

    except Exception as e:
        logger.error("DB error in /customers: %s", e, exc_info=True)
        raise HTTPException(500, f"Database error: {e}")


# ---------------------------------------------------------------------------
# Call logs
# ---------------------------------------------------------------------------

@app.post("/api/call-log")
async def create_call_log(payload: CallLogRequest):
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO call_logs (customer_name, room_name, outcome, duration_seconds, notes)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (payload.customer_name, payload.room_name,
                     payload.outcome, payload.duration_seconds, payload.notes),
                )
                # Update customer call_status
                status_map = {
                    "promise_to_pay": "completed",
                    "completed":      "completed",
                    "not_reachable":  "failed",
                    "refused":        "failed",
                }
                new_status = status_map.get(payload.outcome, "completed")
                await cur.execute(
                    "UPDATE customers SET call_status = %s WHERE customer_name = %s",
                    (new_status, payload.customer_name),
                )
        logger.info("Call log recorded: customer=%s outcome=%s duration=%ss",
                    payload.customer_name, payload.outcome, payload.duration_seconds)
        return {"message": "Call log recorded", "status": new_status}
    except Exception as e:
        logger.error("Failed to record call log: %s", e, exc_info=True)
        raise HTTPException(500, f"Failed to record call log: {e}")


@app.get("/api/call-logs")
async def get_call_logs(
    customer_name: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    pool = _require_pool()
    offset = (page - 1) * limit
    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if customer_name:
                    await cur.execute(
                        "SELECT COUNT(*) AS total FROM call_logs WHERE customer_name = %s",
                        (customer_name,),
                    )
                    total = (await cur.fetchone())["total"]
                    await cur.execute(
                        """SELECT * FROM call_logs WHERE customer_name = %s
                           ORDER BY started_at DESC LIMIT %s OFFSET %s""",
                        (customer_name, limit, offset),
                    )
                else:
                    await cur.execute("SELECT COUNT(*) AS total FROM call_logs")
                    total = (await cur.fetchone())["total"]
                    await cur.execute(
                        "SELECT * FROM call_logs ORDER BY started_at DESC LIMIT %s OFFSET %s",
                        (limit, offset),
                    )
                rows = await cur.fetchall()

        # Make datetimes JSON-serialisable
        for r in rows:
            if isinstance(r.get("started_at"), datetime):
                r["started_at"] = r["started_at"].isoformat()

        return {"logs": rows, "total": total, "page": page, "limit": limit}
    except Exception as e:
        logger.error("DB error in /api/call-logs: %s", e, exc_info=True)
        raise HTTPException(500, f"Database error: {e}")


# ---------------------------------------------------------------------------
# Session — create LiveKit room + dispatch agent
# ---------------------------------------------------------------------------

@app.post("/api/session")
async def create_session(request: SessionRequest):
    pool = _require_pool()

    customer_name = request.name.strip()
    if not customer_name:
        raise HTTPException(400, "Customer name is required")

    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM customers WHERE customer_name = %s", (customer_name,)
                )
                customer = await cur.fetchone()

        if not customer:
            raise HTTPException(404, f"Customer '{customer_name}' not found")

        customer_data = {
            "Customer Name":              customer["customer_name"],
            "Customer Number":            customer["customer_number"],
            "Loan Amount":                customer["loan_amount"],
            "Total Installment":          customer["total_installment"],
            "Cost per Installment":       customer["cost_per_installment"],
            "No of Installment Paid":     customer["no_of_installment_paid"],
            "Last Installment Paid on":   customer["last_installment_paid_on"],
            "Installment Left":           customer["installment_left"],
            "Amount to be Paid":          customer["amount_to_be_paid"],
            "Install Due Date":           customer["install_due_date"],
            "Fine for Late Dues":         customer["fine_for_late_dues"],
        }

        # Unique room per call attempt (avoids duplicate-room conflicts)
        room_name = f"call-{customer['customer_number']}-{secrets.token_hex(4)}"
        identity = f"user-{secrets.token_hex(6)}"
        display_name = clean_display_name(customer_name)
        token = build_token(room_name, identity, display_name)

        async with api.LiveKitAPI(
            url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET
        ) as lk_api:
            await lk_api.room.create_room(
                CreateRoomRequest(
                    name=room_name,
                    metadata=json.dumps(customer_data),
                    empty_timeout=90,
                )
            )
            await lk_api.agent_dispatch.create_dispatch(
                CreateAgentDispatchRequest(
                    room=room_name,
                    agent_name=LIVEKIT_AGENT_NAME,
                )
            )

        # Mark customer as in_progress
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE customers SET call_status = 'in_progress' WHERE customer_name = %s",
                    (customer_name,),
                )

        logger.info("Session created: room=%s customer=%s", room_name, customer_name)
        return {
            "roomName":            room_name,
            "serverUrl":           LIVEKIT_URL,
            "token":               token,
            "participantIdentity": identity,
            "participantName":     display_name,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Session creation error: %s", e, exc_info=True)
        raise HTTPException(500, f"Failed to create session: {e}")


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------

@app.get("/voice-call")
async def voice_call():
    return FileResponse(STATIC_DIR / "voice_call.html", media_type="text/html")


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")