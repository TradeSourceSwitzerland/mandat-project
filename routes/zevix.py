import os
import psycopg
from psycopg.rows import dict_row
import bcrypt
from flask import Blueprint, jsonify, request
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

    return psycopg.connect(
        f"{DATABASE_URL}?sslmode=require",
        row_factory=dict_row
    )

# ----------------------------
# INIT DB (lightweight)
# ----------------------------
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Erstellung der Tabelle 'users', wenn sie nicht existiert
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    plan TEXT,
                    valid_until BIGINT
                );
            """)

            # Erstellung der Tabelle 'usage', wenn sie nicht existiert
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
        return jsonify({"success": False}), 400

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (email,password) VALUES (%s,%s)",
                    (email, hashed)
                )
            conn.commit()

        return jsonify({"success": True})

    except psycopg.errors.UniqueViolation:
        return jsonify({"success": False, "message": "exists"}), 400


# ----------------------------
# LOGIN
# ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:

            # Benutzer anhand der E-Mail abrufen
            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()

            if not user:
                return jsonify({"success": False}), 404

            # Passwort-Überprüfung
            if not bcrypt.checkpw(password.encode(), user["password"].encode()):
                return jsonify({"success": False}), 401

            # auth_until auf 30 Tage setzen, falls nicht gesetzt
            auth_until = user.get("valid_until")
            if not auth_until:
                auth_until = int((datetime.now().timestamp() + 30*24*60*60) * 1000)

            month = datetime.now().strftime("%Y-%m")

            # Überprüfen, ob der Benutzer für den aktuellen Monat schon Daten hat
            cur.execute("""
                INSERT INTO usage (user_email,month,used)
                VALUES (%s,%s,0)
                ON CONFLICT (user_email,month) DO NOTHING
            """, (email, month))

            # Abrufen der verbrauchten Leads für den aktuellen Monat
            cur.execute(
                "SELECT used FROM usage WHERE user_email=%s AND month=%s",
                (email, month)
            )
            used = cur.fetchone()["used"]

        conn.commit()

    # Rückgabe der Login-Daten, einschließlich verbrauchter Leads
    return jsonify({
        "success": True,
        "email": email,
        "plan": user.get("plan"),
        "auth_until": auth_until,
        "month": month,
        "used": used  # Gebe die verbrauchten Leads zurück
    })


# ----------------------------
# OPTIONAL ONE-TIME MIGRATION
# ----------------------------
@zevix_bp.route("/__fix_db_once")
def fix_db_once():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='users' AND column_name='plan'
            """)

            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'basic'")
                conn.commit()
                return jsonify({"status": "created"})

    return jsonify({"status": "ok"})


# ----------------------------
# INIT DB ON IMPORT
# ----------------------------
try:
    print("Initializing ZEVIX DB...")
    init_db()
    print("DB ready")
except Exception as e:
    print("DB INIT FAILED:", e)
