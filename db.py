"""db.py — All async MySQL database operations for OutboundAI."""

import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import aiomysql

from config import DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME

logger = logging.getLogger("db")

# ---------------------------------------------------------------------------
# Connection pool (initialised at server startup)
# ---------------------------------------------------------------------------
_pool: Optional[aiomysql.Pool] = None


async def init_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
            db=DB_NAME, autocommit=True, minsize=2, maxsize=20, charset="utf8mb4",
        )
        logger.info("DB pool created (minsize=2, maxsize=20)")
    return _pool


def get_pool() -> aiomysql.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
        logger.info("DB pool closed")


# ---------------------------------------------------------------------------
# Schema bootstrap — safe to re-run (IF NOT EXISTS everywhere)
# ---------------------------------------------------------------------------
async def ensure_tables(pool: aiomysql.Pool) -> None:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # ── Users (Authentication) ────────────────────────────────────
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

            # ── Customers (existing — preserve) ───────────────────────────
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
                    call_status ENUM('pending','in_progress','completed','failed') DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) CHARACTER SET utf8mb4
            """)
            # Migration: add call_status if missing
            await cur.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='customers' AND COLUMN_NAME='call_status'
            """)
            if (await cur.fetchone())[0] == 0:
                await cur.execute("""ALTER TABLE customers ADD COLUMN call_status
                    ENUM('pending','in_progress','completed','failed') DEFAULT 'pending'""")

            # ── Appointments ──────────────────────────────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS appointments (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    date VARCHAR(20) NOT NULL,
                    time VARCHAR(10) NOT NULL,
                    service VARCHAR(255) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'booked',
                    calcom_booking_uid VARCHAR(100),
                    created_at VARCHAR(30) NOT NULL
                ) CHARACTER SET utf8mb4
            """)

            # ── Call logs (spec version — extended) ───────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS call_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    phone_number VARCHAR(50),
                    customer_name VARCHAR(255),
                    lead_name VARCHAR(255),
                    room_name VARCHAR(255),
                    outcome VARCHAR(50),
                    reason TEXT,
                    duration_seconds INT,
                    recording_url TEXT,
                    notes TEXT,
                    timestamp VARCHAR(30),
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_phone (phone_number),
                    INDEX idx_customer (customer_name),
                    INDEX idx_room (room_name)
                ) CHARACTER SET utf8mb4
            """)
            # Migration: add new columns if missing
            for col, coldef in [
                ("phone_number", "VARCHAR(50)"),
                ("lead_name", "VARCHAR(255)"),
                ("reason", "TEXT"),
                ("recording_url", "TEXT"),
                ("timestamp", "VARCHAR(30)"),
            ]:
                await cur.execute(f"""
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_logs' AND COLUMN_NAME='{col}'
                """)
                if (await cur.fetchone())[0] == 0:
                    await cur.execute(f"ALTER TABLE call_logs ADD COLUMN {col} {coldef}")

            # ── Settings ──────────────────────────────────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    `key` VARCHAR(100) PRIMARY KEY,
                    `value` TEXT NOT NULL,
                    updated_at VARCHAR(30) NOT NULL
                ) CHARACTER SET utf8mb4
            """)

            # ── Error / audit logs ────────────────────────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS error_logs (
                    id VARCHAR(36) PRIMARY KEY,
                    source VARCHAR(50) NOT NULL,
                    level VARCHAR(20) NOT NULL DEFAULT 'error',
                    message TEXT NOT NULL,
                    detail TEXT,
                    timestamp VARCHAR(30) NOT NULL
                ) CHARACTER SET utf8mb4
            """)

            # ── Campaigns ─────────────────────────────────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    contacts_json LONGTEXT NOT NULL,
                    schedule_type VARCHAR(20) NOT NULL DEFAULT 'once',
                    schedule_time VARCHAR(10) DEFAULT '09:00',
                    call_delay_seconds INT DEFAULT 3,
                    system_prompt TEXT,
                    agent_profile_id VARCHAR(36),
                    created_at VARCHAR(30) NOT NULL,
                    last_run_at VARCHAR(30),
                    total_dispatched INT DEFAULT 0,
                    total_failed INT DEFAULT 0
                ) CHARACTER SET utf8mb4
            """)

            # ── Contact memory ────────────────────────────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS contact_memory (
                    id VARCHAR(36) PRIMARY KEY,
                    phone_number VARCHAR(50) NOT NULL,
                    insight TEXT NOT NULL,
                    created_at VARCHAR(30) NOT NULL,
                    INDEX idx_cm_phone (phone_number)
                ) CHARACTER SET utf8mb4
            """)

            # ── Agent profiles ────────────────────────────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_profiles (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    voice VARCHAR(50) NOT NULL DEFAULT 'shubh',
                    voice_model VARCHAR(50) NOT NULL DEFAULT 'bulbul:v3',
                    language_code VARCHAR(10) NOT NULL DEFAULT 'od-IN',
                    model VARCHAR(100) NOT NULL DEFAULT 'sarvam-30b',
                    system_prompt TEXT,
                    enabled_tools TEXT,
                    is_default TINYINT DEFAULT 0,
                    created_at VARCHAR(30) NOT NULL
                ) CHARACTER SET utf8mb4
            """)
            
            # ── Migration: add voice_model and language_code to existing tables ────
            await cur.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'agent_profiles'
                  AND COLUMN_NAME  = 'voice_model'
            """)
            row = await cur.fetchone()
            if row and row[0] == 0:
                # Add missing columns
                await cur.execute("""
                    ALTER TABLE agent_profiles
                    ADD COLUMN voice_model VARCHAR(50) NOT NULL DEFAULT 'bulbul:v3'
                """)
                logger.info("Migration applied: added voice_model column to agent_profiles table")
            
            await cur.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'agent_profiles'
                  AND COLUMN_NAME  = 'language_code'
            """)
            row = await cur.fetchone()
            if row and row[0] == 0:
                await cur.execute("""
                    ALTER TABLE agent_profiles
                    ADD COLUMN language_code VARCHAR(10) NOT NULL DEFAULT 'od-IN'
                """)
                logger.info("Migration applied: added language_code column to agent_profiles table")

            # Dynamic Excel uploads. Column names and row payloads are stored
            # as JSON so each sheet can have its own shape.
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS uploaded_lists (
                    id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    source_filename VARCHAR(255),
                    columns_json LONGTEXT NOT NULL,
                    phone_column VARCHAR(255),
                    lead_name_column VARCHAR(255),
                    row_count INT DEFAULT 0,
                    created_at VARCHAR(30) NOT NULL,
                    updated_at VARCHAR(30) NOT NULL
                ) CHARACTER SET utf8mb4
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS uploaded_list_rows (
                    id VARCHAR(36) PRIMARY KEY,
                    list_id VARCHAR(36) NOT NULL,
                    row_index INT NOT NULL,
                    row_json LONGTEXT NOT NULL,
                    phone_number VARCHAR(50),
                    lead_name VARCHAR(255),
                    call_status VARCHAR(30) DEFAULT 'pending',
                    last_call_room VARCHAR(255),
                    last_call_at VARCHAR(30),
                    call_error TEXT,
                    created_at VARCHAR(30) NOT NULL,
                    INDEX idx_uploaded_rows_list (list_id),
                    INDEX idx_uploaded_rows_phone (phone_number)
                ) CHARACTER SET utf8mb4
            """)

            # ── Agent Conversations ───────────────────────────────────────
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_conversations (
                    id VARCHAR(36) PRIMARY KEY,
                    room_name VARCHAR(255) NOT NULL,
                    speaker VARCHAR(50) NOT NULL,
                    text_content TEXT NOT NULL,
                    timestamp VARCHAR(30) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_conv_room (room_name)
                ) CHARACTER SET utf8mb4
            """)
    logger.info("All database tables verified / created")


# ---------------------------------------------------------------------------
# Sensitive keys — values masked in /api/settings
# ---------------------------------------------------------------------------
SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "SARVAM_API_KEY", "GROQ_API_KEY", "CUSTOM_LLM_API_KEY",
    "VOBIZ_PASSWORD", "TWILIO_AUTH_TOKEN", "DEEPGRAM_API_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY",
}

# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------

async def get_all_settings() -> dict:
    pool = get_pool()
    KNOWN_KEYS = [
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "SARVAM_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER",
        "SARVAM_LLM_MODEL", "GROQ_MODEL",
        "CUSTOM_LLM_BASE_URL", "CUSTOM_LLM_API_KEY", "CUSTOM_LLM_MODEL",
        "SARVAM_STT_MODEL", "SARVAM_TTS_MODEL", "SARVAM_LANGUAGE_CODE",
        "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
        "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
        "DEEPGRAM_API_KEY",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
        "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE",
        "ENABLED_TOOLS",
    ]
    out: dict = {}
    for k in KNOWN_KEYS:
        env_val = os.getenv(k, "")
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": bool(env_val)}
        else:
            out[k] = {"value": env_val, "configured": bool(env_val)}

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT `key`, `value` FROM settings")
            rows = await cur.fetchall()
    for row in rows:
        k, v = row["key"], row["value"]
        if k in SENSITIVE_KEYS:
            out[k] = {"value": "", "configured": bool(v)}
        else:
            out[k] = {"value": v, "configured": bool(v)}
    return out


async def save_settings(data: dict) -> None:
    pool = get_pool()
    now = datetime.now().isoformat()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for k, v in data.items():
                if v is not None and v != "":
                    await cur.execute(
                        "INSERT INTO settings (`key`,`value`,updated_at) VALUES(%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE `value`=VALUES(`value`), updated_at=VALUES(updated_at)",
                        (k, str(v), now),
                    )


async def get_setting(key: str, default: str = "") -> str:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT `value` FROM settings WHERE `key`=%s", (key,))
            row = await cur.fetchone()
    if row:
        return row[0]
    return os.getenv(key, default)


async def set_setting(key: str, value: str) -> None:
    pool = get_pool()
    now = datetime.now().isoformat()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO settings (`key`,`value`,updated_at) VALUES(%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `value`=VALUES(`value`), updated_at=VALUES(updated_at)",
                (key, value, now),
            )


async def get_enabled_tools() -> list:
    raw = await get_setting("ENABLED_TOOLS", "")
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Error / audit logs
# ---------------------------------------------------------------------------

async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO error_logs (id,source,level,message,detail,timestamp) VALUES(%s,%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), source, level, message[:500], detail[:2000], datetime.now().isoformat()),
                )
    except Exception:
        pass


async def get_errors(limit: int = 100) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM error_logs ORDER BY timestamp DESC LIMIT %s", (limit,))
            return await cur.fetchall()


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    pool = get_pool()
    query = "SELECT * FROM error_logs WHERE 1=1"
    params: list = []
    if level:
        query += " AND level=%s"
        params.append(level)
    if source:
        query += " AND source=%s"
        params.append(source)
    query += " ORDER BY timestamp DESC LIMIT %s"
    params.append(limit)
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, params)
            return await cur.fetchall()


async def clear_errors() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM error_logs")


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------

async def insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    full_id = str(uuid.uuid4())
    booking_id = full_id[:8].upper()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO appointments (id,name,phone,date,time,service,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (full_id, name, phone, date, time, service, "booked", datetime.now().isoformat()),
            )
    return booking_id


async def check_slot(date: str, time: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM appointments WHERE date=%s AND time=%s AND status='booked' LIMIT 1",
                (date, time),
            )
            return await cur.fetchone() is None


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18:
            if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
                return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if date_filter:
                await cur.execute("SELECT * FROM appointments WHERE date=%s ORDER BY date,time", (date_filter,))
            else:
                await cur.execute("SELECT * FROM appointments ORDER BY date,time")
            return await cur.fetchall()


async def cancel_appointment(appointment_id: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE appointments SET status='cancelled' WHERE id=%s AND status='booked'",
                (appointment_id,),
            )
            return cur.rowcount > 0


async def get_appointments_by_phone(phone: str) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM appointments WHERE phone=%s ORDER BY date DESC", (phone,))
            return await cur.fetchall()


# ---------------------------------------------------------------------------
# Call logs
# ---------------------------------------------------------------------------

async def log_call(
    phone_number: str, lead_name: Optional[str], outcome: str, reason: str,
    duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None,
    room_name: Optional[str] = None,
) -> None:
    pool = get_pool()
    now = datetime.now().isoformat()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Check if a log with this room_name already exists
            if room_name:
                await cur.execute("SELECT id FROM call_logs WHERE room_name=%s", (room_name,))
                existing = await cur.fetchone()
                if existing:
                    # Update existing
                    await cur.execute(
                        """UPDATE call_logs SET
                           phone_number=%s, customer_name=%s, lead_name=%s, outcome=%s, reason=%s,
                           duration_seconds=%s, recording_url=%s, notes=%s, timestamp=%s
                           WHERE room_name=%s""",
                        (phone_number, lead_name, lead_name, outcome, reason, duration_seconds, recording_url, notes, now, room_name),
                    )
                    return
            # Insert new
            await cur.execute(
                """INSERT INTO call_logs
                   (phone_number,customer_name,lead_name,outcome,reason,duration_seconds,recording_url,notes,timestamp,room_name)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (phone_number, lead_name, lead_name, outcome, reason, duration_seconds, recording_url, notes, now, room_name),
            )


async def get_all_calls(page: int = 1, limit: int = 20) -> list:
    pool = get_pool()
    offset = (page - 1) * limit
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM call_logs ORDER BY COALESCE(timestamp, started_at) DESC LIMIT %s OFFSET %s", (limit, offset))
            rows = await cur.fetchall()
    for r in rows:
        if isinstance(r.get("started_at"), datetime):
            r["started_at"] = r["started_at"].isoformat()
    return rows


async def get_calls_by_phone(phone: str) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM call_logs WHERE phone_number=%s ORDER BY COALESCE(timestamp, started_at) DESC", (phone,))
            rows = await cur.fetchall()
    for r in rows:
        if isinstance(r.get("started_at"), datetime):
            r["started_at"] = r["started_at"].isoformat()
    return rows


async def update_call_notes(call_id: str, notes: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE call_logs SET notes=%s WHERE id=%s", (notes, call_id))
            return cur.rowcount > 0


async def get_contacts() -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM call_logs ORDER BY COALESCE(timestamp, started_at) DESC")
            rows = await cur.fetchall()
    contacts: dict = {}
    for row in rows:
        phone = row.get("phone_number") or row.get("customer_name") or "unknown"
        ts = str(row.get("timestamp") or row.get("started_at") or "")
        if phone not in contacts:
            contacts[phone] = {
                "phone_number": phone, "lead_name": row.get("lead_name") or row.get("customer_name"),
                "total_calls": 0, "booked": 0, "last_call": ts, "last_outcome": row.get("outcome"),
            }
        contacts[phone]["total_calls"] += 1
        if row.get("outcome") == "booked":
            contacts[phone]["booked"] += 1
    return sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def get_stats() -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT outcome, duration_seconds, COALESCE(timestamp, started_at) as ts FROM call_logs")
            rows = await cur.fetchall()

    total_calls = len(rows)
    completed_calls = sum(1 for r in rows if r.get("outcome") == "completed")
    no_answered = sum(1 for r in rows if r.get("outcome") == "no_answer")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    durations = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    avg_dur = sum(durations) / len(durations) if durations else 0

    outcomes: dict = {}
    for r in rows:
        o = r.get("outcome") or "unknown"
        outcomes[o] = outcomes.get(o, 0) + 1

    daily: dict = defaultdict(int)
    for r in rows:
        ts = str(r.get("ts") or "")[:10]
        if ts:
            daily[ts] += 1
    today = datetime.now().date()
    timeline = [
        {"date": (today - timedelta(days=i)).isoformat(), "count": daily.get((today - timedelta(days=i)).isoformat(), 0)}
        for i in range(13, -1, -1)
    ]

    dur_sum: dict = defaultdict(float)
    dur_cnt: dict = defaultdict(int)
    for r in rows:
        o = r.get("outcome") or "unknown"
        sec = r.get("duration_seconds")
        if sec:
            dur_sum[o] += sec
            dur_cnt[o] += 1
    duration_by_outcome = {o: dur_sum[o] / dur_cnt[o] for o in dur_sum}

    return {
        "total_calls": total_calls, "completed_calls": completed_calls, "no_answered": no_answered, "not_interested": not_interested,
        "avg_duration_seconds": round(avg_dur, 1),
        "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": duration_by_outcome,
    }


# ---------------------------------------------------------------------------
# Agent Conversations
# ---------------------------------------------------------------------------

async def save_conversation_event(room_name: str, speaker: str, text: str, timestamp: str) -> None:
    pool = get_pool()
    event_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO agent_conversations (id, room_name, speaker, text_content, timestamp) VALUES (%s, %s, %s, %s, %s)",
                (event_id, room_name, speaker, text, timestamp)
            )


async def get_conversation_history(room_name: str) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM agent_conversations WHERE room_name=%s ORDER BY timestamp ASC", (room_name,))
            return await cur.fetchall()


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

async def create_campaign(
    name: str, contacts_json: str, schedule_type: str = "once",
    schedule_time: str = "09:00", call_delay_seconds: int = 3,
    system_prompt: Optional[str] = None, agent_profile_id: Optional[str] = None,
) -> str:
    cid = str(uuid.uuid4())
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO campaigns
                   (id,name,status,contacts_json,schedule_type,schedule_time,call_delay_seconds,
                    system_prompt,agent_profile_id,created_at,total_dispatched,total_failed)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (cid, name, "active", contacts_json, schedule_type, schedule_time,
                 call_delay_seconds, system_prompt, agent_profile_id,
                 datetime.now().isoformat(), 0, 0),
            )
    return cid


async def get_all_campaigns() -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM campaigns ORDER BY created_at DESC")
            return await cur.fetchall()


async def get_campaign(campaign_id: str) -> Optional[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,))
            return await cur.fetchone()


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE campaigns SET status=%s WHERE id=%s", (status, campaign_id))
            return cur.rowcount > 0


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE campaigns SET last_run_at=%s, total_dispatched=%s, total_failed=%s, status='completed' WHERE id=%s",
                (datetime.now().isoformat(), dispatched, failed, campaign_id),
            )


async def delete_campaign(campaign_id: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM campaigns WHERE id=%s", (campaign_id,))
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Contact memory
# ---------------------------------------------------------------------------

async def add_contact_memory(phone: str, insight: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO contact_memory (id,phone_number,insight,created_at) VALUES(%s,%s,%s,%s)",
                (str(uuid.uuid4()), phone, insight[:1000], datetime.now().isoformat()),
            )


async def get_contact_memory(phone: str) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT insight, created_at FROM contact_memory WHERE phone_number=%s ORDER BY created_at DESC LIMIT 20",
                (phone,),
            )
            return await cur.fetchall()


async def compress_contact_memory(phone: str, compressed: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM contact_memory WHERE phone_number=%s", (phone,))
            await cur.execute(
                "INSERT INTO contact_memory (id,phone_number,insight,created_at) VALUES(%s,%s,%s,%s)",
                (str(uuid.uuid4()), phone, compressed[:2000], datetime.now().isoformat()),
            )


# ---------------------------------------------------------------------------
# Dynamic uploaded lists
# ---------------------------------------------------------------------------

async def create_uploaded_list(
    name: str,
    source_filename: str,
    columns: list,
    rows: list,
    phone_column: Optional[str] = None,
    lead_name_column: Optional[str] = None,
) -> str:
    list_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO uploaded_lists
                   (id,name,source_filename,columns_json,phone_column,lead_name_column,row_count,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    list_id, name, source_filename, json.dumps(columns, ensure_ascii=False),
                    phone_column, lead_name_column, len(rows), now, now,
                ),
            )
            for index, row in enumerate(rows, start=1):
                await cur.execute(
                    """INSERT INTO uploaded_list_rows
                       (id,list_id,row_index,row_json,phone_number,lead_name,call_status,created_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        str(uuid.uuid4()), list_id, index,
                        json.dumps(row.get("data", {}), ensure_ascii=False),
                        row.get("phone_number"), row.get("lead_name"), "pending", now,
                    ),
                )
    return list_id


async def get_uploaded_lists() -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM uploaded_lists ORDER BY created_at DESC")
            rows = await cur.fetchall()
    for row in rows:
        try:
            row["columns"] = json.loads(row.pop("columns_json") or "[]")
        except Exception:
            row["columns"] = []
    return rows


async def get_uploaded_list(list_id: str) -> Optional[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM uploaded_lists WHERE id=%s", (list_id,))
            row = await cur.fetchone()
    if row:
        try:
            row["columns"] = json.loads(row.pop("columns_json") or "[]")
        except Exception:
            row["columns"] = []
    return row


async def update_uploaded_list_mapping(
    list_id: str,
    phone_column: Optional[str],
    lead_name_column: Optional[str],
) -> bool:
    pool = get_pool()
    now = datetime.now().isoformat()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE uploaded_lists
                   SET phone_column=%s, lead_name_column=%s, updated_at=%s
                   WHERE id=%s""",
                (phone_column, lead_name_column, now, list_id),
            )
            return cur.rowcount > 0


async def get_uploaded_list_rows(list_id: str, limit: int = 500, offset: int = 0) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT * FROM uploaded_list_rows
                   WHERE list_id=%s ORDER BY row_index LIMIT %s OFFSET %s""",
                (list_id, limit, offset),
            )
            rows = await cur.fetchall()
    for row in rows:
        try:
            row["data"] = json.loads(row.pop("row_json") or "{}")
        except Exception:
            row["data"] = {}
    return rows


async def get_uploaded_list_row(row_id: str) -> Optional[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM uploaded_list_rows WHERE id=%s", (row_id,))
            row = await cur.fetchone()
    if row:
        try:
            row["data"] = json.loads(row.pop("row_json") or "{}")
        except Exception:
            row["data"] = {}
    return row


async def update_uploaded_list_row_call(
    row_id: str,
    status: str,
    room_name: Optional[str] = None,
    call_error: Optional[str] = None,
) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE uploaded_list_rows
                   SET call_status=%s, last_call_room=%s, last_call_at=%s, call_error=%s
                   WHERE id=%s""",
                (status, room_name, datetime.now().isoformat(), call_error, row_id),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Agent profiles
# ---------------------------------------------------------------------------

async def get_all_agent_profiles() -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM agent_profiles ORDER BY created_at")
            return await cur.fetchall()


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM agent_profiles WHERE id=%s", (profile_id,))
            return await cur.fetchone()


async def create_agent_profile(
    name: str, voice: str = "shubh", voice_model: str = "bulbul:v3", language_code: str = "od-IN",
    model: str = "sarvam-30b", system_prompt: Optional[str] = None, enabled_tools: str = "[]",
    is_default: bool = False,
) -> str:
    pid = str(uuid.uuid4())
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            if is_default:
                await cur.execute("UPDATE agent_profiles SET is_default=0")
            await cur.execute(
                """INSERT INTO agent_profiles (id,name,voice,voice_model,language_code,model,system_prompt,enabled_tools,is_default,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pid, name, voice, voice_model, language_code, model, system_prompt, enabled_tools, 1 if is_default else 0,
                 datetime.now().isoformat()),
            )
    return pid


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    pool = get_pool()
    if not updates:
        return False
    sets = ", ".join(f"`{k}`=%s" for k in updates)
    vals = list(updates.values()) + [profile_id]
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"UPDATE agent_profiles SET {sets} WHERE id=%s", vals)
            return cur.rowcount > 0


async def delete_agent_profile(profile_id: str) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM agent_profiles WHERE id=%s", (profile_id,))
            return cur.rowcount > 0


async def set_default_agent_profile(profile_id: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE agent_profiles SET is_default=0")
            await cur.execute("UPDATE agent_profiles SET is_default=1 WHERE id=%s", (profile_id,))


# ── User Authentication ────────────────────────────────────────────────────────

async def create_user(
    username: str, email: str, password_hash: str, full_name: str, user_type: str = "normal_user"
) -> Optional[dict]:
    """Create a new user."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """INSERT INTO users (username, email, password_hash, full_name, user_type)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (username, email, password_hash, full_name, user_type)
                )
                user_id = cur.lastrowid
                return {
                    "id": user_id,
                    "username": username,
                    "email": email,
                    "full_name": full_name,
                    "user_type": user_type,
                    "is_active": True
                }
            except aiomysql.IntegrityError as e:
                logger.warning(f"User creation failed (duplicate): {e}")
                return None


async def get_user_by_username(username: str) -> Optional[dict]:
    """Get user by username."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, username, email, password_hash, full_name, user_type, is_active FROM users WHERE username=%s",
                (username,)
            )
            return await cur.fetchone()


async def get_user_by_id(user_id: int) -> Optional[dict]:
    """Get user by ID."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, username, email, full_name, user_type, is_active FROM users WHERE id=%s",
                (user_id,)
            )
            return await cur.fetchone()


async def get_all_users() -> list:
    """Get all users."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, username, email, full_name, user_type, is_active, created_at FROM users ORDER BY created_at DESC"
            )
            return await cur.fetchall() or []

# delete user by id
async def delete_user(user_id: int) -> bool:
    """Delete user by ID."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
            return cur.rowcount > 0
        
# update user active status
async def update_user_active_status(user_id: int, is_active: bool) -> bool:
    """Update user active status."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE users SET is_active=%s WHERE id=%s", (is_active, user_id))
            return cur.rowcount > 0        