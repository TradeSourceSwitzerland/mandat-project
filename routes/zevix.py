import os
import json
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

            # USERS TABLE
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    plan TEXT,
                    valid_until BIGINT
                );
            """)

            # USAGE TABLE (Lead Tracking)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    id SERIAL PRIMARY KEY,
                    user_email TEXT NOT NULL,
                    month TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    used_ids JSONB DEFAULT '[]'::jsonb,
                    UNIQUE(user_email, month)
                );
            """)

            # Falls alte Tabelle existiert → Spalte hinzufügen
            cur.execute("""
                ALTER TABLE usage
                ADD COLUMN IF NOT EXISTS used_ids JSONB DEFAULT '[]'::jsonb
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

            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()

            if not user:
                return jsonify({"success": False}), 404

            if not bcrypt.checkpw(password.encode(), user["password"].encode()):
                return jsonify({"success": False}), 401

            auth_until = user.get("valid_until")
            if not auth_until:
                auth_until = int((datetime.now().timestamp() + 30*24*60*60) * 1000)

            month = datetime.now().strftime("%Y-%m")

            cur.execute("""
                INSERT INTO usage (user_email,month,used,used_ids)
                VALUES (%s,%s,0,'[]'::jsonb)
                ON CONFLICT (user_email,month) DO NOTHING
            """, (email, month))

            cur.execute("""
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
            """, (email, month))

            usage = cur.fetchone() or {}
            used = int(usage.get("used") or 0)
            used_ids = usage.get("used_ids") or []

        conn.commit()

    return jsonify({
        "success": True,
        "email": email,
        "plan": user.get("plan"),
        "auth_until": auth_until,
        "month": month,
        "used": used,
        "used_ids": used_ids
    })


# ----------------------------
# LEAD CONSUMPTION (REAL FIX)
# ----------------------------
@zevix_bp.route("/zevix/consume-leads", methods=["POST"])
def consume_leads():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    lead_ids = data.get("lead_ids") or []

    if not email:
        return jsonify({"success": False}), 400

    month = datetime.now().strftime("%Y-%m")

    normalized_ids = [
        str(i).strip().lower()
        for i in lead_ids if str(i).strip()
    ]

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
            """, (email, month))

            row = cur.fetchone()

            if not row:
                stored_ids = set()
                used = 0
            else:
                stored_ids = set(row.get("used_ids") or [])
                used = int(row.get("used") or 0)

            newly_used = 0

            for lid in normalized_ids:
                if lid not in stored_ids:
                    stored_ids.add(lid)
                    newly_used += 1

            new_used = used + newly_used

            cur.execute("""
                INSERT INTO usage (user_email,month,used,used_ids)
                VALUES (%s,%s,%s,%s::jsonb)
                ON CONFLICT (user_email,month)
                DO UPDATE SET used=%s, used_ids=%s::jsonb
            """, (
                email, month, new_used, json.dumps(list(stored_ids)),
                new_used, json.dumps(list(stored_ids))
            ))

        conn.commit()

    return jsonify({
        "success": True,
        "used": new_used,
        "newly_used": newly_used
    })


# ----------------------------
# INIT DB ON IMPORT
# ----------------------------
try:
    print("Initializing ZEVIX DB...")
    init_db()
    print("DB ready")
except Exception as e:
    print("DB INIT FAILED:", e)
