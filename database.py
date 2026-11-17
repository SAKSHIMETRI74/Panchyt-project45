import sqlite3
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from werkzeug.security import generate_password_hash
from config import Config

_lock = threading.Lock()
_ready = False


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_str(dt=None):
    return (dt or utc_now()).strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    Path(Config.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Config.DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_db():
    """Return a connection, guaranteeing the schema exists first."""
    global _ready
    if not _ready:
        init_db()
    return _connect()


def init_db():
    """Create tables and seed data. Safe to call many times."""
    global _ready
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.DatabaseError:
            pass
        _create_schema(conn)
        seed_initial_data(conn)
        conn.close()
        _ready = True


def _create_schema(conn):
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone VARCHAR(10) UNIQUE NOT NULL,
        full_name VARCHAR(100) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(20) DEFAULT 'CITIZEN',
        ward_number VARCHAR(40) DEFAULT 'Ward-1',
        false_strikes INTEGER DEFAULT 0,
        is_blocked BOOLEAN DEFAULT 0,
        failed_login_attempts INTEGER DEFAULT 0,
        lockout_until TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS phone_otps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone VARCHAR(10) NOT NULL,
        otp_hash VARCHAR(255) NOT NULL,
        attempts INTEGER DEFAULT 0,
        resend_count INTEGER DEFAULT 1,
        is_verified BOOLEAN DEFAULT 0,
        verification_token VARCHAR(64) UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL
    );""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_phone_otps ON phone_otps(phone, is_verified);")

    c.execute("""
    CREATE TABLE IF NOT EXISTS complaints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tracking_id VARCHAR(30) UNIQUE NOT NULL,
        citizen_id INTEGER NOT NULL,
        title VARCHAR(150) NOT NULL,
        category VARCHAR(50) NOT NULL,
        description TEXT NOT NULL,
        severity VARCHAR(20) DEFAULT 'Medium',
        status VARCHAR(30) DEFAULT 'Pending',
        photo_filename VARCHAR(255),
        latitude REAL,
        longitude REAL,
        location_address VARCHAR(255),
        ai_confidence REAL DEFAULT 0.0,
        ai_category VARCHAR(50),
        ai_flagged BOOLEAN DEFAULT 0,
        ai_notes TEXT,
        assigned_to VARCHAR(100),
        officer_remarks TEXT,
        is_false_complaint BOOLEAN DEFAULT 0,
        resolved_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (citizen_id) REFERENCES users(id) ON DELETE CASCADE
    );""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tracking_id ON complaints(tracking_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_complaint_status ON complaints(status, severity);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_complaint_citizen ON complaints(citizen_id);")

    c.execute("""
    CREATE TABLE IF NOT EXISTS complaint_audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complaint_id INTEGER NOT NULL,
        actor_id INTEGER NOT NULL,
        actor_role VARCHAR(30) NOT NULL,
        action VARCHAR(60) NOT NULL,
        previous_state TEXT,
        new_state TEXT,
        justification TEXT NOT NULL,
        ip_address VARCHAR(45),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
    );""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS complaint_appeals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complaint_id INTEGER NOT NULL,
        citizen_id INTEGER NOT NULL,
        appeal_reason TEXT NOT NULL,
        evidence_filename VARCHAR(255),
        status VARCHAR(30) DEFAULT 'PENDING',
        reviewed_by INTEGER,
        review_notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TIMESTAMP,
        FOREIGN KEY (complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
    );""")
    conn.commit()


def seed_initial_data(conn):
    c = conn.cursor()

    # 1. Official (PDO) account
    c.execute("SELECT id FROM users WHERE phone = ?", (Config.DEFAULT_ADMIN_PHONE,))
    if not c.fetchone():
        password = Config.DEFAULT_ADMIN_PASSWORD
        generated = False
        if not password:
            password = secrets.token_urlsafe(10)
            generated = True
        c.execute(
            "INSERT INTO users (phone, full_name, password_hash, role, ward_number) "
            "VALUES (?, ?, ?, 'ADMIN', 'Panchayat Bhavan')",
            (Config.DEFAULT_ADMIN_PHONE, Config.DEFAULT_ADMIN_NAME, generate_password_hash(password)),
        )
        if generated:
            print("=" * 60)
            print(f"[ADMIN] Official login created. Phone: {Config.DEFAULT_ADMIN_PHONE}")
            print(f"[ADMIN] Generated password: {password}")
            print("[ADMIN] Set DEFAULT_ADMIN_PASSWORD in your environment to choose your own.")
            print("=" * 60)
    conn.commit()

    if not Config.SEED_DEMO_DATA:
        return

    # 2. Demo citizen
    c.execute("SELECT id FROM users WHERE phone = ?", (Config.DEMO_CITIZEN_PHONE,))
    row = c.fetchone()
    if row:
        citizen_id = row["id"]
    else:
        c.execute(
            "INSERT INTO users (phone, full_name, password_hash, role, ward_number) "
            "VALUES (?, 'Basavaraju Gowda (ಬಸವರಾಜು ಗೌಡ)', ?, 'CITIZEN', 'Ward-3 (North)')",
            (Config.DEMO_CITIZEN_PHONE, generate_password_hash(Config.DEMO_CITIZEN_PASSWORD)),
        )
        citizen_id = c.lastrowid

    # 3. Demo complaints (only when the table is (almost) empty)
    c.execute("SELECT COUNT(*) FROM complaints")
    if c.fetchone()[0] >= 6:
        conn.commit()
        return

    now = utc_str()
    year = datetime.now().year
    # Spread demo records over past days so they look real (and don't count toward today's limit)
    days_ago = {"1001": 21, "1002": 18, "1003": 15, "1004": 12, "1005": 7,
                "1006": 5, "1007": 4, "1008": 3, "1009": 2}
    demo = [
        # Resolved
        ("1001", "Broken drinking water distribution pipe", "Drinking Water",
         "Continuous water leakage at the Ward-3 main valve near Anjaneya temple.",
         "Critical", "Resolved", 12.9716, 77.5946, "Main Road, Ward-3",
         "Ramesh (Panchayat Plumber)", "Replaced PVC junction section. Water pressure normalized.", now),
        ("1002", "Burnt streetlights near Government School", "Streetlights",
         "Three streetlights off on school lane causing safety issues for students.",
         "Medium", "Resolved", 12.9780, 77.5900, "School Road, Ward-3",
         "Anand (Gram Electrician)", "Replaced fused LED fixture and junction breaker.", now),
        ("1003", "Choked stormwater culvert causing flooding", "Sanitation & Drainage",
         "Solid debris choked the culvert channel near the dairy cooperative.",
         "High", "Resolved", 12.9650, 77.6010, "Cross-4 Road, Ward-3",
         "Suresh Sanitation Team", "JCB cleared culvert channel. Rainwater flow restored.", now),
        ("1004", "Handpump borewell handle detachment", "Drinking Water",
         "Borewell handle broken near community hall. 35 families affected.",
         "High", "Resolved", 12.9700, 77.5980, "Community Hall, Ward-3",
         "Ramesh Plumber", "Replaced iron linkage rod and greased pivot.", now),
        # In progress
        ("1005", "Road pothole patch work near Market Cross", "Village Roads",
         "Deep road trench after monsoon rain making two-wheelers skid.",
         "High", "In Progress", 12.9740, 77.5920, "Market Road, Ward-2",
         "Mahesh (Civil Works Contractor)", "Gravel foundation laid. Bitumen leveling in progress.", None),
        ("1006", "Open drainage slab broken near bus stop", "Sanitation & Drainage",
         "Reinforced cement slab broken, danger of pedestrians falling.",
         "Critical", "In Progress", 12.9760, 77.5950, "Main Bus Stop, Ward-1",
         "Kiran (Engineering Team)", "New precast RCC slab delivered. Installation underway.", None),
        ("1007", "Electric wire sparking near transformer", "Streetlights",
         "Overhead cable touching tree branch sparking during wind.",
         "Critical", "In Progress", 12.9690, 77.5910, "Post Office Road, Ward-2",
         "BESCOM / Gram Lineman", "Tree trimming team dispatched. Line isolation underway.", None),
        # Pending
        ("1008", "Solid waste accumulation near weekly market", "Sanitation & Drainage",
         "Debris not cleared after Saturday market causing foul smell.",
         "Medium", "Pending", 12.9730, 77.5990, "Weekly Market Ground, Ward-1", None, None, None),
        ("1009", "New streetlight request at East Village dead-end", "Streetlights",
         "Dead-end lane has no light post, total darkness at night.",
         "Low", "Pending", 12.9810, 77.5890, "East Village Lane 4", None, None, None),
    ]

    for (num, title, cat, desc, sev, status, lat, lng, addr, who, remarks, resolved) in demo:
        tracking_id = f"GP-{year}-{num}"
        c.execute("SELECT id FROM complaints WHERE tracking_id = ?", (tracking_id,))
        if c.fetchone():
            continue
        created = utc_str(utc_now() - timedelta(days=days_ago[num]))
        resolved_at = utc_str(utc_now() - timedelta(days=max(1, days_ago[num] - 3))) if resolved else None
        c.execute("""
            INSERT INTO complaints (
                tracking_id, citizen_id, title, category, description, severity, status,
                latitude, longitude, location_address, ai_confidence, ai_category,
                ai_flagged, ai_notes, assigned_to, officer_remarks, resolved_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.93, ?, 0, 'Demo record.', ?, ?, ?, ?, ?)
        """, (tracking_id, citizen_id, title, cat, desc, sev, status, lat, lng, addr,
              cat, who, remarks, resolved_at, created, created))
        cid = c.lastrowid
        c.execute("""
            INSERT INTO complaint_audit_logs (
                complaint_id, actor_id, actor_role, action, previous_state, new_state,
                justification, ip_address, created_at
            ) VALUES (?, ?, 'SYSTEM', 'INITIAL_RECORD', 'None', ?, 'Grievance record created', '127.0.0.1', ?)
        """, (cid, citizen_id, status, created))
    conn.commit()
