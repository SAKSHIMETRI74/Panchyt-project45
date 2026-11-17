import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# DATA_DIR is where the SQLite file and uploaded photos live.
# On Render, mount a persistent disk (e.g. /var/data) and set DATA_DIR=/var/data
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))

# Running on Render / production?
IS_PROD = bool(os.environ.get("RENDER")) or os.environ.get("FLASK_ENV") == "production"


class Config:
    # ---- Flask security ----
    SECRET_KEY = (
        os.environ.get("SECRET_KEY")
        or os.environ.get("FLASK_SECRET_KEY")
        or secrets.token_hex(32)  # random fallback (sessions reset on restart)
    )
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

    # ---- Storage ----
    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(DATA_DIR / "panchayat.db"))
    PRIVATE_UPLOAD_FOLDER = str(DATA_DIR / "private_uploads")
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_MB", 8)) * 1024 * 1024
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

    # ---- Session cookies ----
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = IS_PROD
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24  # 24 hours

    # ---- Jurisdiction (default: Karnataka bounding box) ----
    GEO_BOUNDS = {"min_lat": 11.0, "max_lat": 19.0, "min_lng": 73.5, "max_lng": 79.0}

    # ---- Accounts ----
    DEFAULT_ADMIN_PHONE = os.environ.get("DEFAULT_ADMIN_PHONE", "9876543210")
    # Locally a known default is used. In production you MUST set DEFAULT_ADMIN_PASSWORD;
    # otherwise a random one is generated and printed once in the logs.
    DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD") or (
        None if IS_PROD else "PanchayatAdmin@2026"
    )
    DEFAULT_ADMIN_NAME = os.environ.get("DEFAULT_ADMIN_NAME", "Panchayat Development Officer (PDO)")

    # ---- Demo data (sample citizen + sample complaints so dashboards are not empty) ----
    SEED_DEMO_DATA = os.environ.get("SEED_DEMO_DATA", "1") == "1"
    DEMO_CITIZEN_PHONE = "9845012345"
    DEMO_CITIZEN_PASSWORD = os.environ.get("DEMO_CITIZEN_PASSWORD", "Citizen@123")

    # ---- Services ----
    # mock      -> OTP is shown on screen (DEMO ONLY, anyone can register any number)
    # fast2sms  -> real SMS, needs FAST2SMS_API_KEY
    SMS_PROVIDER = os.environ.get("SMS_GATEWAY_PROVIDER", "mock")

    # Shown in the page header (set in Render env vars), e.g. "1800-xxx-xxxx"
    HELPLINE_NUMBER = os.environ.get("HELPLINE_NUMBER", "")
