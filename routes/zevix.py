import os
import psycopg
from psycopg.rows import dict_row
import bcrypt
from flask import Blueprint, jsonify, request, send_file
from datetime import datetime
import pandas as pd
import tempfile

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

    return psycopg.connect(
        f"{DATABASE_URL}?sslmode=require",
        row_factory=dict_row
    )


# ----------------------------
# LOAD LOCAL EXCEL FILES
# ----------------------------

def load_all_leads():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(project_root, "data")

    if not os.path.exists(data_dir):
        raise RuntimeError(f"/data folder missing at {data_dir}")

    files = [
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.endswith(".xlsx")
    ]

    if not files:
        raise RuntimeError("No Excel files inside /data")

    dfs = []

    for f in files:
        try:
            print(f"Reading file: {f}")

            # Versuch 1: normal lesen
            df = pd.read_excel(f)

            # Wenn leer → typische SHAB Struktur (Header später)
            if df.empty:
                df = pd.read_excel(f, header=2)

            # Wenn immer noch leer → alle Sheets testen
            if df.empty:
                xls = pd.ExcelFile(f)
                for sheet in xls.sheet_names:
                    df = pd.read_excel(xls, sheet_name=sheet, header=2)
                    if not df.empty:
                        break

            print(f"Loaded rows: {len(df)}")

            if not df.empty:
                dfs.append(df)

        except Exception as e:
            print(f"Error reading {f}: {e}")

    if not dfs:
        print("⚠️ No data extracted from Excel files")
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    print(f"TOTAL LEADS LOADED: {len(combined)}")

    return combined

# ----------------------------
# INIT DB (Render-safe lazy init)
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

_db_ready = False

@zevix_bp.before_app_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True


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

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            "INSERT INTO users (email,password,plan,valid_until) VALUES (%s,%s,NULL,NULL)",
            (email, hashed)
        )
        conn.commit()
    except psycopg.errors.UniqueViolation:
        return jsonify({"success": False, "message": "User exists"}), 400
    finally:
        cur.close()
        conn.close()

    return jsonify({"success": True})


# ----------------------------
# LOGIN
# ----------------------------

@zevix_bp.route("/zevix/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cur.fetchone()

    if not user:
        return jsonify({"success": False}), 404

    if not bcrypt.checkpw(password.encode(), user["password"].encode()):
        return jsonify({"success": False}), 401

    plan = user["plan"]

    auth_until = user["valid_until"]
    if not auth_until:
        auth_until = int((datetime.now().timestamp() + 30*24*60*60) * 1000)

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
    used = cur.fetchone()["used"]

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
# EXPORT LEADS (Abo Pflicht)
# ----------------------------

PLAN_LIMITS = {
    "basic": 500,
    "business": 1000,
    "enterprise": 4500
}

@zevix_bp.route("/zevix/export/<email>", methods=["GET"])
def export(email):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT plan FROM users WHERE email=%s", (email,))
    user = cur.fetchone()

    if not user or not user["plan"]:
        return jsonify({"success": False, "message": "no_subscription"}), 403

    plan = user["plan"]
    monthly_limit = PLAN_LIMITS.get(plan, 500)

    month = datetime.now().strftime("%Y-%m")

    cur.execute("""
        SELECT used FROM usage WHERE user_email=%s AND month=%s
    """, (email, month))
    result = cur.fetchone()

    used = result["used"] if result else 0

    if used >= monthly_limit:
        return jsonify({"success": False, "message": "limit_reached"}), 403


    # 🔐 LOAD LOCAL DATA (SECURE)
    df = load_all_leads()

    df_export = df.sample(min(50, len(df)))

    cur.execute("""
        UPDATE usage SET used = used + %s
        WHERE user_email=%s AND month=%s
    """, (len(df_export), email, month))

    conn.commit()
    cur.close()
    conn.close()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    df_export.to_excel(tmp.name, index=False)

    return send_file(tmp.name, as_attachment=True, download_name="leads.xlsx")

# ----------------------------
# LEAD STATS (für Dashboard)
# ----------------------------

@zevix_bp.route("/zevix/stats", methods=["GET"])
def stats():
    try:
        df = load_all_leads()
        total = len(df)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({
        "success": True,
        "total_leads": int(total)
    })
