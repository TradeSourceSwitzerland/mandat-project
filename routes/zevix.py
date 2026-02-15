import os
import psycopg
from psycopg.rows import dict_row
import bcrypt
from flask import Blueprint, jsonify, request, send_file
from datetime import datetime
import pandas as pd
import tempfile

# ----------------------------
# DATABASE CONNECTION
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    return psycopg.connect(f"{DATABASE_URL}?sslmode=require", row_factory=dict_row)

# ----------------------------
# INITIALIZE DATABASE
# ----------------------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # USERS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            plan TEXT DEFAULT 'basic',
            valid_until BIGINT
        );
    """)

    # USAGE (pro Monat)
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

init_db()

# ----------------------------
# BLUEPRINT
# ----------------------------
zevix_bp = Blueprint("zevix", __name__)

# ----------------------------
# HEALTH CHECK
# ----------------------------
@zevix_bp.route("/zevix/health", methods=["GET"])
def zevix_health():
    return jsonify({"status": "ZEVIX backend running"})

# ----------------------------
# REGISTER
# ----------------------------
@zevix_bp.route("/zevix/register", methods=["POST"])
def register():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"success": False}), 400

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            "INSERT INTO users (email, password) VALUES (%s, %s)",
            (email, hashed.decode())
        )
        conn.commit()
    except:
        return jsonify({"success": False, "message": "User exists"}), 400
    finally:
        cur.close()
        conn.close()

    return jsonify({"success": True})

# ----------------------------
# LOGIN (liefert Werte fürs bestehende Dashboard!)
# ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def zevix_login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cur.fetchone()

    if not user:
        return jsonify({"success": False}), 404

    stored = user["password"].encode()
    if not bcrypt.checkpw(password.encode(), stored):
        return jsonify({"success": False}), 401

    # Plan bestimmen
    plan = user["plan"] or "basic"

    # Ablaufdatum (30 Tage wenn noch keines gesetzt)
    auth_until = user["valid_until"]
    if not auth_until:
        auth_until = int((datetime.now().timestamp() + 30*24*60*60) * 1000)

    # Monat bestimmen
    month = datetime.now().strftime("%Y-%m")

    # Usage sicherstellen
    cur.execute("""
        INSERT INTO usage (user_email, month, used)
        VALUES (%s, %s, 0)
        ON CONFLICT (user_email, month) DO NOTHING
    """, (email, month))

    cur.execute(
        "SELECT used FROM usage WHERE user_email=%s AND month=%s",
        (email, month)
    )
    used = cur.fetchone()["used"]

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
# LIMITS
# ----------------------------
PLAN_LIMITS = {
    "basic": 500,
    "business": 1000,
    "enterprise": 4500
}

# ----------------------------
# EXPORT LEADS (Excel bleibt Quelle!)
# ----------------------------
@zevix_bp.route("/zevix/export/<email>", methods=["GET"])
def export_leads(email):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT plan FROM users WHERE email=%s", (email,))
    user = cur.fetchone()

    if not user:
        return jsonify({"success": False}), 404

    plan = user["plan"] or "basic"
    monthly_limit = PLAN_LIMITS.get(plan, 500)

    month = datetime.now().strftime("%Y-%m")

    cur.execute("""
        INSERT INTO usage (user_email, month, used)
        VALUES (%s, %s, 0)
        ON CONFLICT (user_email, month) DO NOTHING
    """, (email, month))

    cur.execute(
        "SELECT used FROM usage WHERE user_email=%s AND month=%s",
        (email, month)
    )
    used = cur.fetchone()["used"]

    if used >= monthly_limit:
        return jsonify({"success": False, "message": "Limit reached"}), 403

    EXPORT_SIZE = 50

    df = pd.read_excel("data/master.xlsx")
    df_export = df.sample(min(EXPORT_SIZE, len(df)))

    cur.execute("""
        UPDATE usage
        SET used = used + %s
        WHERE user_email=%s AND month=%s
    """, (len(df_export), email, month))

    conn.commit()
    cur.close()
    conn.close()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    df_export.to_excel(tmp.name, index=False)

    response = send_file(tmp.name, as_attachment=True, download_name="leads.xlsx")

    # Frontend kann damit localStorage syncen wenn nötig
    response.headers["X-Month"] = month
    response.headers["X-New-Used"] = str(used + len(df_export))

    return response
