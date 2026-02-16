import os
import psycopg
from psycopg.rows import dict_row
import bcrypt
from flask import Blueprint, jsonify, request, send_file
from datetime import datetime

# ----------------------------
# CONFIG
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

# ----------------------------
# DATABASE CONNECTION
# ----------------------------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing")

    # Render Postgres: SSL required
    return psycopg.connect(
        f"{DATABASE_URL}?sslmode=require",
        row_factory=dict_row
    )

# ----------------------------
# INIT DB (safe, lightweight)
# ----------------------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            plan TEXT,
            valid_until BIGINT
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            month TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            UNIQUE(user_email, month)
        );
    """)

    conn.commit()
    cur.close()
    conn.close()

# ----------------------------
# BLUEPRINT
# ----------------------------
zevix_bp = Blueprint("zevix", __name__)

# ----------------------------
# REGISTER
# ----------------------------
@zevix_bp.route("/zevix/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "missing_fields"}), 400

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    conn = get_conn()
    cur = conn.cursor()

    try:
        # plan/valid_until bleiben erstmal NULL
        cur.execute(
            "INSERT INTO users (email,password,plan,valid_until) VALUES (%s,%s,NULL,NULL)",
            (email, hashed)
        )
        conn.commit()
        return jsonify({"success": True})
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"success": False, "message": "user_exists"}), 400
    finally:
        cur.close()
        conn.close()

# ----------------------------
# LOGIN
# ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "missing_fields"}), 400

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cur.fetchone()

    if not user:
        cur.close(); conn.close()
        return jsonify({"success": False, "message": "not_found"}), 404

    if not bcrypt.checkpw(password.encode("utf-8"), user["password"].encode("utf-8")):
        cur.close(); conn.close()
        return jsonify({"success": False, "message": "wrong_password"}), 401

    plan = user.get("plan")  # kann None sein
    auth_until = user.get("valid_until")

    # fallback: 30 Tage Session
    if not auth_until:
        auth_until = int((datetime.now().timestamp() + 30 * 24 * 60 * 60) * 1000)

    month = datetime.now().strftime("%Y-%m")

    # ensure usage row exists
    cur.execute("""
        INSERT INTO usage (user_email,month,used)
        VALUES (%s,%s,0)
        ON CONFLICT (user_email,month) DO NOTHING
    """, (email, month))

    cur.execute(
        "SELECT used FROM usage WHERE user_email=%s AND month=%s",
        (email, month)
    )
    row = cur.fetchone()
    used = row["used"] if row else 0

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "success": True,
        "email": email,
        "plan": plan,
        "auth_until": auth_until,
        "month": month,
        "used": used
    })

# ----------------------------
# EXPORT LEADS (STREAM XLSX ONLY - NO PANDAS!)
# ----------------------------
@zevix_bp.route("/zevix/export/<path:email>", methods=["GET"])
def export(email):
    email = (email or "").strip().lower()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT plan FROM users WHERE email=%s", (email,))
    user = cur.fetchone()

    if not user or not user.get("plan"):
        cur.close(); conn.close()
        return jsonify({"success": False, "message": "no_subscription"}), 403

    # ✅ KEIN Excel lesen, KEIN RAM, nur Datei streamen
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(project_root, "data")

    if not os.path.exists(data_dir):
        cur.close(); conn.close()
        return jsonify({"success": False, "error": "data_folder_missing"}), 500

    files = sorted([f for f in os.listdir(data_dir) if f.lower().endswith(".xlsx")])

    if not files:
        cur.close(); conn.close()
        return jsonify({"success": False, "error": "no_excel_files"}), 500

    # wenn du mehrere Files hast: nimm das erste (oder ändere Logik)
    file_path = os.path.join(data_dir, files[0])

    cur.close()
    conn.close()

    # Browser lädt XLSX und filtert selbst
    return send_file(
        file_path,
        as_attachment=True,
        download_name="leads.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# ----------------------------
# TEMP DB MIGRATION (RUN ONCE THEN DELETE)
# ----------------------------
@zevix_bp.route("/__fix_db_once")
def fix_db_once():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='users' AND column_name='plan'
        """)
        exists = cur.fetchone()

        if not exists:
            cur.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'basic'")
            conn.commit()
            return jsonify({"status": "plan column CREATED"})

        return jsonify({"status": "plan column already exists"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

# ----------------------------
# INIT DB ON IMPORT (FAST, SAFE)
# ----------------------------
try:
    print("🔵 Initializing database...")
    init_db()
    print("✅ Database ready")
except Exception as e:
    # Do not crash the process hard; log it
    print("❌ DB INIT FAILED:", e)
