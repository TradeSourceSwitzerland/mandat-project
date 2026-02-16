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
VALID_PLANS = {"none", "basic", "business", "enterprise"}


# ----------------------------
# HELPERS
# ----------------------------
def normalize_plan(plan):
    p = str(plan or "none").strip().lower()
    return p if p in VALID_PLANS else "none"


def default_auth_until_ms():
    return int((datetime.now().timestamp() + 30 * 24 * 60 * 60) * 1000)


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

            # Saubere Defaults für bestehende Nutzer
            cur.execute("""
                UPDATE users
                SET plan='none'
                WHERE plan IS NULL OR trim(plan)=''
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
                    "INSERT INTO users (email,password,plan) VALUES (%s,%s,%s)",
                    (email, hashed, "none")
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
            auth_until = user.get("valid_until") or default_auth_until_ms()
            plan = normalize_plan(user.get("plan"))

            # Persistiere saubere Defaults
            cur.execute(
                "UPDATE users SET plan=%s, valid_until=COALESCE(valid_until, %s) WHERE email=%s",
                (plan, auth_until, email)
            )

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
        "plan": plan,
        "auth_until": auth_until,
        "month": month,
        "used": used,
        "used_ids": used_ids
    })


# ----------------------------
# LEAD CONSUMPTION (REAL FIX)
# SUBSCRIPTION UPDATE (for checkout success pages)
# ----------------------------
@zevix_bp.route("/zevix/update-subscription", methods=["POST"])
def update_subscription():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    plan = normalize_plan(data.get("plan"))
    auth_until = data.get("auth_until")

    if not email:
        return jsonify({"success": False, "message": "email_missing"}), 400

    if plan == "none":
        return jsonify({"success": False, "message": "invalid_plan"}), 400

    try:
        auth_until = int(auth_until) if auth_until is not None else default_auth_until_ms()
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "invalid_auth_until"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET plan=%s, valid_until=%s
                WHERE email=%s
                RETURNING email
                """,
                (plan, auth_until, email)
            )
            updated = cur.fetchone()

            if not updated:
                return jsonify({"success": False, "message": "user_not_found"}), 404

        conn.commit()

    return jsonify({
        "success": True,
        "email": email,
        "plan": plan,
        "auth_until": auth_until
    })


# ----------------------------
# LEAD CONSUMPTION
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
@@ -192,39 +268,102 @@ def consume_leads():
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
        "month": month,
        "used": new_used,
        "used_ids": list(stored_ids),
        "newly_used": newly_used
    })


# ----------------------------
# SESSION SYNC (Dashboard Refresh)
# ----------------------------
@zevix_bp.route("/zevix/session-sync", methods=["POST"])
def session_sync():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"success": False}), 400

    month = datetime.now().strftime("%Y-%m")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT plan, valid_until FROM users WHERE email=%s", (email,))
            user = cur.fetchone()

            if not user:
                return jsonify({"success": False}), 404

            auth_until = user.get("valid_until") or default_auth_until_ms()
            plan = normalize_plan(user.get("plan"))

            cur.execute(
                "UPDATE users SET plan=%s, valid_until=COALESCE(valid_until, %s) WHERE email=%s",
                (plan, auth_until, email)
            )

            cur.execute(
                """
                INSERT INTO usage (user_email,month,used,used_ids)
                VALUES (%s,%s,0,'[]'::jsonb)
                ON CONFLICT (user_email,month) DO NOTHING
                """,
                (email, month)
            )

            cur.execute(
                """
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
                """,
                (email, month)
            )
            usage = cur.fetchone() or {}

        conn.commit()

    return jsonify({
        "success": True,
        "email": email,
        "plan": plan,
        "auth_until": auth_until,
        "month": month,
        "used": int(usage.get("used") or 0),
        "used_ids": usage.get("used_ids") or []
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
