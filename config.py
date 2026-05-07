"""config.py — Centralised configuration loaded from environment variables."""
from __future__ import annotations

import datetime as dt
import os
import re

from dotenv import load_dotenv
from livekit import api

load_dotenv()

# ── App ───────────────────────────────────────────────────────────────────────
APP_TITLE = os.getenv("APP_TITLE", "OutboundAI Dashboard")

# ── LiveKit ───────────────────────────────────────────────────────────────────
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
LIVEKIT_AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "outbound-caller")
LIVEKIT_ROOM_PREFIX = os.getenv("LIVEKIT_ROOM_PREFIX", "voice-web")
LIVEKIT_EMPTY_TIMEOUT = int(os.getenv("LIVEKIT_EMPTY_TIMEOUT", "90"))

# ── LLM ───────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
SARVAM_LLM_MODEL = os.getenv("SARVAM_LLM_MODEL", "sarvam-30b")

# ── Custom LLM ────────────────────────────────────────────────────────────────
CUSTOM_LLM_BASE_URL = os.getenv("CUSTOM_LLM_BASE_URL", "")
CUSTOM_LLM_API_KEY = os.getenv("CUSTOM_LLM_API_KEY", "")
CUSTOM_LLM_MODEL = os.getenv("CUSTOM_LLM_MODEL", "")

# ── STT / TTS ─────────────────────────────────────────────────────────────────
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3")
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_LANGUAGE_CODE = os.getenv("SARVAM_LANGUAGE_CODE", "od-IN")

# ── Sarvam TTS Voice Models & Voices ──────────────────────────────────────────
# Voice Models
SARVAM_VOICE_MODELS = {
    "bulbul:v3": {
        "label": "Bulbul v3 (Premium - 37 voices)",
        "voices": {
            "shubh": "🎤 Shubh (Default)",
            "aditya": "🎤 Aditya",
            "ritu": "🎤 Ritu",
            "priya": "🎤 Priya",
            "neha": "🎤 Neha",
            "rahul": "🎤 Rahul",
            "pooja": "🎤 Pooja",
            "rohan": "🎤 Rohan",
            "simran": "🎤 Simran",
            "kavya": "🎤 Kavya",
            "amit": "🎤 Amit",
            "dev": "🎤 Dev",
            "ishita": "🎤 Ishita",
            "shreya": "🎤 Shreya",
            "ratan": "🎤 Ratan",
            "varun": "🎤 Varun",
            "manan": "🎤 Manan",
            "sumit": "🎤 Sumit",
            "roopa": "🎤 Roopa",
            "kabir": "🎤 Kabir",
            "aayan": "🎤 Aayan",
            "ashutosh": "🎤 Ashutosh",
            "advait": "🎤 Advait",
            "anand": "🎤 Anand",
            "tanya": "🎤 Tanya",
            "tarun": "🎤 Tarun",
            "sunny": "🎤 Sunny",
            "mani": "🎤 Mani",
            "gokul": "🎤 Gokul",
            "vijay": "🎤 Vijay",
            "shruti": "🎤 Shruti",
            "suhani": "🎤 Suhani",
            "mohit": "🎤 Mohit",
            "kavitha": "🎤 Kavitha",
            "rehan": "🎤 Rehan",
            "soham": "🎤 Soham",
            "rupali": "🎤 Rupali",
        }
    },
    "bulbul:v2": {
        "label": "Bulbul v2 (Standard - 7 voices)",
        "voices": {
            "anushka": "🎤 Anushka (Female)",
            "manisha": "🎤 Manisha (Female)",
            "vidya": "🎤 Vidya (Female)",
            "arya": "🎤 Arya (Female)",
            "abhilash": "🎤 Abhilash (Male)",
            "karun": "🎤 Karun (Male)",
            "hitesh": "🎤 Hitesh (Male)",
        }
    }
}

# Language Codes (BCP-47 format)
SARVAM_LANGUAGE_CODES = [
    ("bn-IN", "Bengali (India)"),
    ("en-IN", "English (India)"),
    ("gu-IN", "Gujarati (India)"),
    ("hi-IN", "Hindi (India)"),
    ("kn-IN", "Kannada (India)"),
    ("ml-IN", "Malayalam (India)"),
    ("mr-IN", "Marathi (India)"),
    ("od-IN", "Odia (India)"),
    ("pa-IN", "Punjabi (India)"),
    ("ta-IN", "Tamil (India)"),
    ("te-IN", "Telugu (India)"),
]

# ── Database ──────────────────────────────────────────────────────────────────
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME", "ai_voice_agent")

# ── Agent Optimization ────────────────────────────────────────────────────────
VAD_MIN_SILENCE_DURATION = float(os.getenv("VAD_MIN_SILENCE_DURATION", "0.2"))
VAD_MIN_ENDPOINTING_DELAY = float(os.getenv("VAD_MIN_ENDPOINTING_DELAY", "0.2"))

# ── Vobiz SIP ─────────────────────────────────────────────────────────────────
VOBIZ_SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")
VOBIZ_USERNAME = os.getenv("VOBIZ_USERNAME", "")
VOBIZ_PASSWORD = os.getenv("VOBIZ_PASSWORD", "")
VOBIZ_OUTBOUND_NUMBER = os.getenv("VOBIZ_OUTBOUND_NUMBER", "")
OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID", "")
DEFAULT_TRANSFER_NUMBER = os.getenv("DEFAULT_TRANSFER_NUMBER", "")

# ── Twilio SMS (optional) ────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# ── S3 Recording (optional) ──────────────────────────────────────────────────
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")
S3_REGION = os.getenv("S3_REGION", "ap-south-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")

# ── Cal.com (optional) ───────────────────────────────────────────────────────
CALCOM_API_KEY = os.getenv("CALCOM_API_KEY", "")
CALCOM_EVENT_TYPE_ID = os.getenv("CALCOM_EVENT_TYPE_ID", "")
CALCOM_TIMEZONE = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")

# ── CORS ──────────────────────────────────────────────────────────────────────
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000")
ALLOWED_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# ── Utilities ─────────────────────────────────────────────────────────────────
_NAME_PATTERN = re.compile(r"[^A-Za-z0-9 _-]+")


def ensure_env(*keys: str) -> None:
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def clean_display_name(name: str | None) -> str:
    value = _NAME_PATTERN.sub("", (name or "").strip())
    return value[:40] or "Guest"


def build_token(room_name: str, identity: str, display_name: str) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    return (
        api.AccessToken(api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(display_name)
        .with_ttl(dt.timedelta(hours=2))
        .with_grants(grants)
        .to_jwt()
    )
