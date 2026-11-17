#!/usr/bin/env python3
"""
Gram Panchayat Digital Grievance Portal
Bilingual (English + Kannada) | Citizen portal + Official portal
Run locally:  python app.py
Run on Render: gunicorn app:app --workers 1 --threads 4 --preload
"""
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask, abort, g, jsonify, redirect, render_template, request,
    send_from_directory, session, url_for,
)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from ai_verifier import inspect_and_sanitize
from config import Config
from database import get_db, init_db, utc_now, utc_str
from otp_service import INDIAN_PHONE_REGEX, OTPService
from translations import get_text

app = Flask(__name__)
app.config.from_object(Config)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # correct IP/https behind Render

UPLOAD_DIR = Path(Config.PRIVATE_UPLOAD_FOLDER)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

init_db()  # create tables + seed once at start-up (get_db() also guarantees this)
otp_service = OTPService(Config.SMS_PROVIDER)

OFFICIAL_ROLES = ("ADMIN", "SECRETARY", "STAFF")
STATUSES = ("Pending", "In Progress", "Resolved", "Rejected")

CATEGORIES = [
    ("Drinking Water", "cat_water"),
    ("Village Roads", "cat_roads"),
    ("Streetlights", "cat_lights"),
    ("Sanitation & Drainage", "cat_sanitation"),
    ("Public Health", "cat_health"),
    ("Other", "cat_other"),
]
SEVERITIES = [
    ("Critical", "sev_critical"),
    ("High", "sev_high"),
    ("Medium", "sev_medium"),
    ("Low", "sev_low"),
]
STATUS_KEYS = {
    "Pending": "status_pending", "In Progress": "status_progress",
    "Resolved": "status_resolved", "Rejected": "status_rejected",
}
CATEGORY_KEYS = dict(CATEGORIES)
SEVERITY_KEYS = dict(SEVERITIES)
WARD_CHOICES = [f"Ward-{i}" for i in range(1, 13)]


# ----------------------------------------------------
# Small helpers
# ----------------------------------------------------
_rate_lock = threading.Lock()
_rate_hits = {}


def rate_limited(key, limit, window_seconds):
    """Tiny in-memory limiter (per server process)."""
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return True
        hits.append(now)
        _rate_hits[key] = hits
        if len(_rate_hits) > 5000:  # keep memory bounded
            for k in list(_rate_hits)[:1000]:
                _rate_hits.pop(k, None)
    return False


def client_ip():
    return request.remote_addr or "unknown"


def json_error(message, status=400, **extra):
    payload = {"success": False, "message": message}
    payload.update(extra)
    return jsonify(payload), status


def clean_phone(raw):
    return OTPService.clean_phone(raw)


def get_current_user():
    if "current_user" in g:
        return g.current_user
    user = None
    user_id = session.get("user_id")
    if user_id:
        conn = get_db()
        user = conn.execute(
            "SELECT id, phone, full_name, role, ward_number, false_strikes, is_blocked "
            "FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        conn.close()
        if user is None:
            session.clear()
    g.current_user = user
    return user


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_current_user():
            if request.path.startswith("/api/"):
                return json_error("Please sign in first.", 401)
            return redirect(url_for("citizen_login_view"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or user["role"] not in OFFICIAL_ROLES:
            if request.path.startswith("/api/"):
                return json_error("Official authorization required.", 403)
            return redirect(url_for("admin_login_view"))
        return f(*args, **kwargs)
    return decorated


def start_session(user_row):
    lang = session.get("lang")
    session.clear()  # prevents session fixation
    if lang:
        session["lang"] = lang
    session.permanent = True
    session["user_id"] = user_row["id"]
    session["user_name"] = user_row["full_name"]


def add_audit(conn, complaint_id, actor_id, actor_role, action, prev, new, justification):
    conn.execute(
        "INSERT INTO complaint_audit_logs (complaint_id, actor_id, actor_role, action, "
        "previous_state, new_state, justification, ip_address) VALUES (?,?,?,?,?,?,?,?)",
        (complaint_id, actor_id, actor_role, action, prev, new, justification or "-", client_ip()),
    )


def new_tracking_id(conn):
    year = datetime.now().year
    for _ in range(20):
        tid = f"GP-{year}-{secrets.randbelow(900000) + 100000}"
        if not conn.execute("SELECT 1 FROM complaints WHERE tracking_id = ?", (tid,)).fetchone():
            return tid
    return f"GP-{year}-{uuid.uuid4().hex[:8].upper()}"


# ----------------------------------------------------
# Template context & response headers
# ----------------------------------------------------
@app.context_processor
def inject_globals():
    lang = session.get("lang", "en")
    return {
        "lang": lang,
        "t": lambda key: get_text(key, lang),
        "current_user": get_current_user(),
        "OFFICIAL_ROLES": OFFICIAL_ROLES,
        "CATEGORIES": CATEGORIES,
        "SEVERITIES": SEVERITIES,
        "STATUS_KEYS": STATUS_KEYS,
        "CATEGORY_KEYS": CATEGORY_KEYS,
        "SEVERITY_KEYS": SEVERITY_KEYS,
        "WARD_CHOICES": WARD_CHOICES,
        "helpline": Config.HELPLINE_NUMBER,
        "demo_sms": otp_service.is_mock,
    }


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(self)")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; form-action 'self'",
    )
    if request.path.startswith(("/api/", "/citizen", "/admin", "/media")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(RequestEntityTooLarge)
def too_large(_e):
    return json_error("File is too large. Please upload a photo under 8 MB.", 413)


@app.errorhandler(HTTPException)
def http_error(e):
    if request.path.startswith("/api/"):
        return json_error(e.description or e.name, e.code)
    return render_template("error.html", code=e.code, name=e.name), e.code


@app.errorhandler(Exception)
def server_error(e):
    app.logger.exception("Unhandled error: %s", e)
    if request.path.startswith("/api/"):
        return json_error("Server error. Please try again.", 500)
    return render_template("error.html", code=500, name="Server Error"), 500


# ----------------------------------------------------
# Public pages
# ----------------------------------------------------
@app.route("/healthz")
def healthz():
    conn = get_db()
    conn.execute("SELECT 1")
    conn.close()
    return "ok", 200


@app.route("/")
def index():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(status='Resolved'),0) AS resolved, "
        "COALESCE(SUM(status='In Progress'),0) AS in_progress, "
        "COALESCE(SUM(status='Pending'),0) AS pending FROM complaints"
    ).fetchone()
    conn.close()
    return render_template("landing.html", stats=dict(row))


@app.route("/set-language/<lang>")
def set_language(lang):
    if lang in ("en", "kn"):
        session["lang"] = lang
    ref = request.referrer or ""
    if ref.startswith(request.host_url):
        return redirect(ref)
    return redirect(url_for("index"))


@app.route("/track")
def track_view():
    return render_template("track.html", query=request.args.get("q", "").strip()[:40])


@app.route("/logout")
def logout():
    lang = session.get("lang")
    session.clear()
    if lang:
        session["lang"] = lang
    return redirect(url_for("index"))


# ----------------------------------------------------
# Citizen gateway
# ----------------------------------------------------
@app.route("/citizen/login")
def citizen_login_view():
    user = get_current_user()
    if user:
        return redirect(url_for("admin_dashboard" if user["role"] in OFFICIAL_ROLES else "citizen_dashboard"))
    return render_template("citizen_login.html")


@app.route("/citizen/dashboard")
@login_required
def citizen_dashboard():
    user = get_current_user()
    if user["role"] in OFFICIAL_ROLES:
        return redirect(url_for("admin_dashboard"))
    conn = get_db()
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM complaint_appeals a WHERE a.complaint_id = c.id "
        "AND a.status = 'PENDING') AS pending_appeals "
        "FROM complaints c WHERE citizen_id = ? ORDER BY created_at DESC, id DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    complaints = [dict(r) for r in rows]
    stats = {
        "total": len(complaints),
        "resolved": sum(c["status"] == "Resolved" for c in complaints),
        "in_progress": sum(c["status"] == "In Progress" for c in complaints),
        "pending": sum(c["status"] == "Pending" for c in complaints),
    }
    return render_template("citizen_dashboard.html", complaints=complaints, stats=stats)


@app.route("/citizen/statement")
@login_required
def citizen_statement():
    user = get_current_user()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM complaints WHERE citizen_id = ? ORDER BY created_at DESC, id DESC", (user["id"],)
    ).fetchall()
    conn.close()
    return render_template(
        "statement.html", complaints=[dict(r) for r in rows],
        statement_date=datetime.now().strftime("%d %B %Y"),
    )


# ----------------------------------------------------
# Official gateway
# ----------------------------------------------------
@app.route("/admin/login")
def admin_login_view():
    user = get_current_user()
    if user and user["role"] in OFFICIAL_ROLES:
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_login.html")


@app.route("/admin")
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    conn = get_db()
    complaints = [dict(r) for r in conn.execute("""
        SELECT c.*, u.full_name AS citizen_name, u.phone AS citizen_phone, u.false_strikes
        FROM complaints c JOIN users u ON c.citizen_id = u.id
        ORDER BY CASE c.status WHEN 'Resolved' THEN 2 WHEN 'Rejected' THEN 2 ELSE 1 END,
                 CASE c.severity WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END,
                 c.created_at DESC
    """).fetchall()]
    appeals = [dict(r) for r in conn.execute("""
        SELECT a.*, c.tracking_id, c.title AS complaint_title, c.officer_remarks AS complaint_remarks,
               u.full_name AS citizen_name, u.phone AS citizen_phone
        FROM complaint_appeals a
        JOIN complaints c ON a.complaint_id = c.id
        JOIN users u ON a.citizen_id = u.id
        WHERE a.status = 'PENDING' ORDER BY a.created_at ASC
    """).fetchall()]
    conn.close()
    stats = {
        "total": len(complaints),
        "resolved": sum(c["status"] == "Resolved" for c in complaints),
        "in_progress": sum(c["status"] == "In Progress" for c in complaints),
        "pending": sum(c["status"] == "Pending" for c in complaints),
    }
    return render_template("admin_dashboard.html", complaints=complaints, appeals=appeals, stats=stats)


# ----------------------------------------------------
# Auth APIs
# ----------------------------------------------------
@app.route("/api/auth/otp/send", methods=["POST"])
def send_otp():
    data = request.get_json(silent=True) or {}
    if rate_limited(f"otp:{client_ip()}", 8, 900):
        return json_error("Too many requests. Please wait a few minutes.", 429)
    conn = get_db()
    ok, payload = otp_service.request_otp(conn, data.get("phone", ""))
    conn.close()
    if not ok:
        return json_error(payload, 400)
    return jsonify({"success": True, **payload})


@app.route("/api/auth/otp/verify", methods=["POST"])
def verify_otp():
    data = request.get_json(silent=True) or {}
    if rate_limited(f"otpv:{client_ip()}", 20, 900):
        return json_error("Too many attempts. Please wait a few minutes.", 429)
    conn = get_db()
    ok, payload = otp_service.verify_otp(conn, data.get("phone", ""), data.get("otp", ""))
    conn.close()
    if not ok:
        return json_error(payload, 400)
    return jsonify({"success": True, **payload})


@app.route("/api/auth/register", methods=["POST"])
def complete_registration():
    data = request.get_json(silent=True) or {}
    phone = clean_phone(data.get("phone", ""))
    token = str(data.get("verification_token", ""))
    full_name = str(data.get("full_name", "")).strip()[:100]
    password = str(data.get("password", ""))
    ward = str(data.get("ward", "Ward-1"))
    if ward not in WARD_CHOICES:
        ward = "Ward-1"

    if len(full_name) < 3 or len(password) < 6:
        return json_error("Name (min 3 chars) and password (min 6 chars) are required.")
    if not INDIAN_PHONE_REGEX.match(phone):
        return json_error("Invalid mobile number.")

    conn = get_db()
    valid = conn.execute(
        "SELECT id FROM phone_otps WHERE phone = ? AND verification_token = ? AND is_verified = 1 "
        "AND created_at > ?", (phone, token, utc_str(utc_now() - timedelta(minutes=30))),
    ).fetchone()
    if not token or not valid:
        conn.close()
        return json_error("Phone verification expired. Please verify again.")

    existing = conn.execute("SELECT id, role FROM users WHERE phone = ?", (phone,)).fetchone()
    if existing:
        # With demo (on-screen) OTP anyone could take over an account, so only allow
        # password reset through OTP when a real SMS provider is configured.
        if existing["role"] != "CITIZEN" or otp_service.is_mock:
            conn.close()
            return json_error("This mobile number is already registered. Please sign in.", 409)
        conn.execute(
            "UPDATE users SET full_name = ?, password_hash = ?, ward_number = ?, "
            "failed_login_attempts = 0, lockout_until = NULL WHERE id = ?",
            (full_name, generate_password_hash(password), ward, existing["id"]),
        )
        user_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO users (phone, full_name, password_hash, role, ward_number) VALUES (?,?,?,'CITIZEN',?)",
            (phone, full_name, generate_password_hash(password), ward),
        )
        user_id = cur.lastrowid

    conn.execute("UPDATE phone_otps SET verification_token = NULL WHERE id = ?", (valid["id"],))
    conn.commit()
    user = conn.execute("SELECT id, full_name FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    start_session(user)
    return jsonify({"success": True, "message": "Registration complete!", "role": "CITIZEN"})


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    phone = clean_phone(data.get("phone", ""))
    password = str(data.get("password", ""))
    portal = data.get("portal", "citizen")

    if rate_limited(f"login:{client_ip()}", 20, 600):
        return json_error("Too many sign-in attempts. Please wait 10 minutes.", 429)

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()

    if user and user["lockout_until"] and user["lockout_until"] > utc_str():
        conn.close()
        return json_error("Too many wrong passwords. Account locked for 15 minutes.", 429)

    if not user or not check_password_hash(user["password_hash"], password):
        if user:
            attempts = (user["failed_login_attempts"] or 0) + 1
            lock = utc_str(utc_now() + timedelta(minutes=15)) if attempts >= 5 else None
            conn.execute(
                "UPDATE users SET failed_login_attempts = ?, lockout_until = ? WHERE id = ?",
                (0 if lock else attempts, lock, user["id"]),
            )
            conn.commit()
        conn.close()
        return json_error("Invalid mobile number or password.", 401)

    conn.execute("UPDATE users SET failed_login_attempts = 0, lockout_until = NULL WHERE id = ?", (user["id"],))
    conn.commit()
    conn.close()

    is_official = user["role"] in OFFICIAL_ROLES
    if portal == "admin" and not is_official:
        return json_error("This account is not an official account.", 403)
    if user["is_blocked"]:
        return json_error("Account restricted due to repeated false complaints. Contact the Panchayat office.", 403)

    start_session(user)
    return jsonify({
        "success": True, "message": "Welcome!", "role": user["role"],
        "redirect": url_for("admin_dashboard" if is_official else "citizen_dashboard"),
    })


# ----------------------------------------------------
# Complaints
# ----------------------------------------------------
@app.route("/api/complaints/submit", methods=["POST"])
@login_required
def submit_complaint():
    user = get_current_user()
    if user["role"] != "CITIZEN":
        return json_error("Only citizens can file grievances.", 403)
    if user["is_blocked"]:
        return json_error("Your account is restricted.", 403)

    title = request.form.get("title", "").strip()[:150]
    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()[:3000]
    severity = request.form.get("severity", "Medium").strip()
    address = request.form.get("address", "").strip()[:255]

    if len(title) < 5 or len(description) < 15:
        return json_error("Title (min 5 chars) and description (min 15 chars) are required.")
    if category not in CATEGORY_KEYS:
        return json_error("Please choose a valid category.")
    if severity not in SEVERITY_KEYS:
        severity = "Medium"

    latitude = longitude = None
    lat, lng = request.form.get("latitude"), request.form.get("longitude")
    if lat and lng:
        try:
            latitude, longitude = float(lat), float(lng)
        except ValueError:
            return json_error("Invalid GPS location.")
        b = Config.GEO_BOUNDS
        if not (b["min_lat"] <= latitude <= b["max_lat"] and b["min_lng"] <= longitude <= b["max_lng"]):
            return json_error("GPS location is outside this Panchayat's region.")

    conn = get_db()
    today = utc_str(utc_now() - timedelta(days=1))
    recent = conn.execute(
        "SELECT COUNT(*) FROM complaints WHERE citizen_id = ? AND created_at > ?", (user["id"], today)
    ).fetchone()[0]
    if recent >= 10:
        conn.close()
        return json_error("Daily limit reached (10 grievances per day). Please try again tomorrow.", 429)

    photo_filename = None
    ai = {"confidence": 0.0, "category": category, "notes": "No photo attached."}
    photo = request.files.get("photo")
    if photo and photo.filename:
        ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
        if ext not in Config.ALLOWED_EXTENSIONS:
            conn.close()
            return json_error("Unsupported format. Please upload a JPG, PNG or WEBP photo.")
        tmp_path = UPLOAD_DIR / f"tmp_{uuid.uuid4().hex}"
        final_name = f"{uuid.uuid4().hex}.jpg"
        try:
            photo.save(tmp_path)
            result = inspect_and_sanitize(str(tmp_path), str(UPLOAD_DIR / final_name), category)
        finally:
            tmp_path.unlink(missing_ok=True)
        if not result["ok"]:
            conn.close()
            return json_error(
                f"Photo not accepted: {result['reason']}. Please upload a clear photo of the problem.",
                400, photo_rejected=True,
            )
        photo_filename = final_name
        ai = result

    tracking_id = new_tracking_id(conn)
    cur = conn.execute("""
        INSERT INTO complaints (tracking_id, citizen_id, title, category, description, severity, status,
            photo_filename, latitude, longitude, location_address, ai_confidence, ai_category, ai_flagged, ai_notes)
        VALUES (?,?,?,?,?,?,'Pending',?,?,?,?,?,?,0,?)
    """, (tracking_id, user["id"], title, category, description, severity, photo_filename,
          latitude, longitude, address, ai.get("confidence", 0.0), ai.get("category", category),
          ai.get("notes", "")))
    add_audit(conn, cur.lastrowid, user["id"], "CITIZEN", "COMPLAINT_FILED", "None", "Pending",
              "Citizen filed grievance")
    conn.commit()
    conn.close()
    return jsonify({"success": True, "tracking_id": tracking_id,
                    "message": f"Grievance registered. Your Tracking ID is {tracking_id}"})


@app.route("/api/complaints/track/<tracking_id>")
def track_complaint(tracking_id):
    """Public lookup by Tracking ID only. Returns no personal data."""
    if rate_limited(f"track:{client_ip()}", 40, 60):
        return json_error("Too many lookups. Please wait a minute.", 429)
    tid = tracking_id.strip().upper()[:40]
    conn = get_db()
    c = conn.execute(
        "SELECT id, tracking_id, title, category, severity, status, location_address, created_at, "
        "updated_at, resolved_at, officer_remarks FROM complaints WHERE tracking_id = ?", (tid,)
    ).fetchone()
    if not c:
        conn.close()
        return json_error("No record found for this Tracking ID.", 404)
    timeline = [dict(r) for r in conn.execute(
        "SELECT new_state AS status, created_at FROM complaint_audit_logs "
        "WHERE complaint_id = ? AND action IN ('COMPLAINT_FILED','INITIAL_RECORD','STATUS_UPDATE','APPEAL_UPHELD') "
        "ORDER BY created_at ASC, id ASC", (c["id"],)).fetchall()]
    conn.close()
    data = dict(c)
    data.pop("id")
    return jsonify({"success": True, "complaint": data, "timeline": timeline})


@app.route("/media/complaint/<int:complaint_id>/<filename>")
@login_required
def serve_complaint_photo(complaint_id, filename):
    user = get_current_user()
    conn = get_db()
    comp = conn.execute(
        "SELECT citizen_id, photo_filename FROM complaints WHERE id = ?", (complaint_id,)
    ).fetchone()
    conn.close()
    if not comp or comp["photo_filename"] != filename:
        abort(404)
    if user["id"] != comp["citizen_id"] and user["role"] not in OFFICIAL_ROLES:
        abort(403)
    return send_from_directory(str(UPLOAD_DIR), filename, mimetype="image/jpeg")


@app.route("/api/complaints/<int:complaint_id>/appeal", methods=["POST"])
@login_required
def submit_appeal(complaint_id):
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or request.form.get("reason", "")).strip()[:2000]
    if len(reason) < 15:
        return json_error("Please provide a detailed reason (min 15 characters).")

    conn = get_db()
    comp = conn.execute(
        "SELECT id, citizen_id, status FROM complaints WHERE id = ?", (complaint_id,)
    ).fetchone()
    if not comp or comp["citizen_id"] != user["id"]:
        conn.close()
        return json_error("Grievance not found.", 404)
    if comp["status"] != "Rejected":
        conn.close()
        return json_error("Appeals can only be filed against rejected grievances.")
    if conn.execute("SELECT 1 FROM complaint_appeals WHERE complaint_id = ? AND status = 'PENDING'",
                    (complaint_id,)).fetchone():
        conn.close()
        return json_error("An appeal for this grievance is already under review.", 409)

    conn.execute("INSERT INTO complaint_appeals (complaint_id, citizen_id, appeal_reason) VALUES (?,?,?)",
                 (complaint_id, user["id"], reason))
    add_audit(conn, complaint_id, user["id"], "CITIZEN", "APPEAL_FILED", comp["status"], comp["status"], reason)
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Appeal submitted to the Panchayat Development Officer."})


# ----------------------------------------------------
# Official APIs
# ----------------------------------------------------
@app.route("/api/admin/complaints/<int:complaint_id>/update", methods=["POST"])
@admin_required
def admin_update_status(complaint_id):
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    assignee = str(data.get("assigned_to", "")).strip()[:100]
    remarks = str(data.get("officer_remarks", "")).strip()[:1000]
    mark_false = bool(data.get("mark_false"))
    user = get_current_user()

    if new_status not in STATUSES:
        return json_error("Invalid status.")
    if new_status == "Rejected" and len(remarks) < 10:
        return json_error("Please give a reason (min 10 characters) when rejecting a grievance.")
    if mark_false and new_status != "Rejected":
        return json_error("Only rejected grievances can be marked as false.")

    conn = get_db()
    comp = conn.execute("SELECT status, citizen_id, is_false_complaint FROM complaints WHERE id = ?",
                        (complaint_id,)).fetchone()
    if not comp:
        conn.close()
        return json_error("Grievance not found.", 404)

    resolved_at = utc_str() if new_status == "Resolved" else None
    conn.execute("""
        UPDATE complaints SET status = ?, assigned_to = ?, officer_remarks = ?,
            is_false_complaint = ?, resolved_at = COALESCE(?, resolved_at),
            updated_at = CURRENT_TIMESTAMP WHERE id = ?
    """, (new_status, assignee, remarks, 1 if mark_false else 0, resolved_at, complaint_id))

    note = ""
    if mark_false and not comp["is_false_complaint"]:
        u = conn.execute("SELECT false_strikes FROM users WHERE id = ?", (comp["citizen_id"],)).fetchone()
        strikes = (u["false_strikes"] or 0) + 1
        block = 1 if strikes >= 3 else 0
        conn.execute("UPDATE users SET false_strikes = ?, is_blocked = ? WHERE id = ?",
                     (strikes, block, comp["citizen_id"]))
        note = f" Strike {strikes}/3 recorded." + (" Account restricted." if block else "")
        add_audit(conn, complaint_id, user["id"], user["role"], "FALSE_COMPLAINT_STRIKE",
                  str(strikes - 1), str(strikes), remarks)

    add_audit(conn, complaint_id, user["id"], user["role"], "STATUS_UPDATE", comp["status"],
              new_status, remarks or "Status updated by official")
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Updated." + note})


@app.route("/api/admin/appeals/<int:appeal_id>/decide", methods=["POST"])
@admin_required
def admin_decide_appeal(appeal_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    notes = str(data.get("notes", "")).strip()[:1000]
    user = get_current_user()
    if decision not in ("UPHELD", "REJECTED"):
        return json_error("Invalid decision.")
    if len(notes) < 10:
        return json_error("Please add decision notes (min 10 characters).")

    conn = get_db()
    appeal = conn.execute(
        "SELECT complaint_id, citizen_id, status FROM complaint_appeals WHERE id = ?", (appeal_id,)
    ).fetchone()
    if not appeal:
        conn.close()
        return json_error("Appeal not found.", 404)
    if appeal["status"] != "PENDING":
        conn.close()
        return json_error("This appeal has already been decided.", 409)

    conn.execute(
        "UPDATE complaint_appeals SET status = ?, reviewed_by = ?, review_notes = ?, "
        "reviewed_at = CURRENT_TIMESTAMP WHERE id = ?", (decision, user["id"], notes, appeal_id))
    if decision == "UPHELD":
        was_false = conn.execute("SELECT is_false_complaint FROM complaints WHERE id = ?",
                                 (appeal["complaint_id"],)).fetchone()
        conn.execute(
            "UPDATE complaints SET status = 'Pending', is_false_complaint = 0, officer_remarks = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (f"Appeal upheld: {notes}", appeal["complaint_id"]))
        if was_false and was_false["is_false_complaint"]:
            conn.execute(
                "UPDATE users SET false_strikes = MAX(0, false_strikes - 1), is_blocked = 0 WHERE id = ?",
                (appeal["citizen_id"],))
        add_audit(conn, appeal["complaint_id"], user["id"], user["role"], "APPEAL_UPHELD",
                  "Rejected", "Pending", notes)
    else:
        add_audit(conn, appeal["complaint_id"], user["id"], user["role"], "APPEAL_REJECTED",
                  "Rejected", "Rejected", notes)
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Appeal {decision.lower()}."})


@app.route("/api/admin/complaints/<int:complaint_id>/audit")
@admin_required
def get_complaint_audit(complaint_id):
    conn = get_db()
    logs = [dict(r) for r in conn.execute("""
        SELECT a.action, a.previous_state, a.new_state, a.justification, a.actor_role,
               a.created_at, u.full_name AS actor_name
        FROM complaint_audit_logs a LEFT JOIN users u ON a.actor_id = u.id
        WHERE a.complaint_id = ? ORDER BY a.created_at ASC, a.id ASC
    """, (complaint_id,)).fetchall()]
    conn.close()
    return jsonify({"success": True, "audit_trail": logs})


# ----------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Gram Panchayat Portal running at http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=Config.DEBUG)
