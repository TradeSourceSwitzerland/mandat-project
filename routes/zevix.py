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

def extract_plan(data):
    """Unterstützt alte/neue Payload-Feldnamen für das Abo."""
    return normalize_plan(
        data.get("plan")
        or data.get("abo")
        or data.get("subscription")
        or data.get("tier")
    )

def extract_email(data):
    """Unterstützt alternative Feldnamen für E-Mail aus Webflow/Stripe Flows."""
    raw = (
        data.get("email")
        or data.get("user_email")
        or data.get("customer_email")
        or data.get("zevix_email")
        or (data.get("customer_details") or {}).get("email")
        or (data.get("metadata") or {}).get("email")
    )
    return (str(raw or "").strip().lower())

def extract_auth_until(data):
    """Unterstützt alternative Feldnamen für Laufzeit."""
    return data.get("auth_until", data.get("valid_until"))

def default_auth_until_ms():
    return int((datetime.now().timestamp() + 30 * 24 * 60 * 60) * 1000)

def get_month_key():
    return datetime.now().strftime("%Y-%m")

# ----------------------------
# DATABASE CONNECTION
# ----------------------------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing")

    # Render Postgres: sslmode=require
    return psycopg.connect(
        f"{DATABASE_URL}?sslmode=require",
        row_factory=dict_row
    )

# ----------------------------
# INIT DB (lightweight + migration safe)
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

            # 🔥 Migration safety (sehr wichtig für Render!)
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS valid_until BIGINT
            """)

            # 🔥 Alte User reparieren (Backfill)
            cur.execute("""
                UPDATE users
                SET valid_until = %s
                WHERE valid_until IS NULL
            """, (default_auth_until_ms(),))

            # USAGE TABLE
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

            # Migration safety (falls Tabelle schon existierte)
            cur.execute("""
                ALTER TABLE usage
                ADD COLUMN IF NOT EXISTS used_ids JSONB DEFAULT '[]'::jsonb
            """)

            # Defaults für existierende Nutzer
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
        return jsonify({"success": False, "message": "missing"}), 400

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, password, plan, valid_until)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (email, hashed, "none", default_auth_until_ms())
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
        return jsonify({"success": False, "message": "missing"}), 400

    month = get_month_key()

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
            if not user:
                return jsonify({"success": False, "message": "not_found"}), 404

            if not bcrypt.checkpw(password.encode(), user["password"].encode()):
                return jsonify({"success": False, "message": "wrong_password"}), 401

            plan = normalize_plan(user.get("plan"))
            auth_until = user.get("valid_until") or default_auth_until_ms()

            # Persistiere Defaults sauber
            cur.execute(
                """
                UPDATE users
                SET plan=%s,
                    valid_until=COALESCE(valid_until, %s)
                WHERE email=%s
                """,
                (plan, auth_until, email)
            )

            # Usage row sicherstellen
            cur.execute(
                """
                INSERT INTO usage (user_email, month, used, used_ids)
                VALUES (%s, %s, 0, '[]'::jsonb)
                ON CONFLICT (user_email, month) DO NOTHING
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
            used = int(usage.get("used") or 0)
            used_ids = usage.get("used_ids") or []

        conn.commit()

    return jsonify({
        "success": True,
        "email": email,
        "plan": plan,
        "auth_until": auth_until,
        "month": month,
        "used": used,
        "used_ids": used_ids
    })

# ----------------------------
# SUBSCRIPTION UPDATE (Checkout success)
# ----------------------------
@zevix_bp.route("/zevix/update-subscription", methods=["POST"])
def update_subscription():
    data = request.get_json(silent=True) or {}
    email = extract_email(data)
    plan = extract_plan(data)
    auth_until = extract_auth_until(data)

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
                WHERE lower(email)=lower(%s)
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
# LEAD CONSUMPTION (dedupe by lead_ids)
# ----------------------------
@zevix_bp.route("/zevix/consume-leads", methods=["POST"])
def consume_leads():
    data = request.get_json(silent=True) or {}
    email = extract_email(data)
    lead_ids = data.get("lead_ids")
    if lead_ids is None:
        lead_ids = data.get("ids")
    if lead_ids is None and data.get("lead_id") is not None:
        lead_ids = [data.get("lead_id")]
    if lead_ids is None:
        lead_ids = []

    # Fallback: Einige Clients senden nur eine Anzahl statt IDs.
    count_hint = data.get("count", data.get("used", data.get("verbrauch", 0)))
    try:
        count_hint = max(0, int(count_hint or 0))
    except (TypeError, ValueError):
        count_hint = 0

    if not email:
        return jsonify({"success": False, "message": "email_missing"}), 400

    month = get_month_key()

    # IDs normalisieren
    normalized_ids = []
    for i in lead_ids:
        s = str(i).strip().lower()
        if s:
            normalized_ids.append(s)

    with get_conn() as conn:
        with conn.cursor() as cur:

            # --- USER + PLAN LADEN ---
            cur.execute("SELECT plan FROM users WHERE lower(email)=lower(%s)", (email,))
            user = cur.fetchone()
            if not user:
                return jsonify({"success": False, "message": "user_not_found"}), 404

            plan = normalize_plan(user.get("plan"))

            LIMITS = {
                "basic": 500,
                "business": 1000,
                "enterprise": 4500,
                "none": 0
            }
            limit = LIMITS.get(plan, 0)

            # --- USAGE ROW SICHERSTELLEN ---
            cur.execute("""
                INSERT INTO usage (user_email, month, used, used_ids)
                VALUES (%s, %s, 0, '[]'::jsonb)
                ON CONFLICT (user_email, month) DO NOTHING
            """, (email, month))

            cur.execute("""
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
            """, (email, month))

            row = cur.fetchone() or {}
            used = int(row.get("used") or 0)
            stored_ids = set(row.get("used_ids") or [])

            newly_used = 0
            for lid in normalized_ids:
                if lid not in stored_ids:
                    if used + newly_used >= limit:
                        break  # LIMIT ERREICHT
                    stored_ids.add(lid)
                    newly_used += 1
                    
            # Falls keine IDs gesendet wurden, können wir trotzdem den Verbrauch erhöhen.
            if not normalized_ids and count_hint > 0:
                remaining = max(0, limit - (used + newly_used))
                newly_used += min(count_hint, remaining)

            new_used = used + newly_used

            # --- UPDATE ---
            cur.execute("""
                UPDATE usage
                SET used=%s, used_ids=%s::jsonb
                WHERE user_email=%s AND month=%s
            """, (new_used, json.dumps(list(stored_ids)), email, month))

        conn.commit()

    return jsonify({
        "success": True,
        "plan": plan,  # ← DAS FEHLTE!
        "month": month,
        "used": new_used,
        "used_ids": list(stored_ids),
        "newly_used": newly_used,
        "limit": limit
    })

# ----------------------------
# SESSION SYNC (Dashboard refresh)
# ----------------------------
@zevix_bp.route("/zevix/session-sync", methods=["POST"])
def session_sync():
    data = request.get_json(silent=True) or {}
    email = extract_email(data)

    if not email:
        return jsonify({"success": False, "message": "email_missing"}), 400

    month = get_month_key()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT plan, valid_until FROM users WHERE lower(email)=lower(%s)", (email,))
            user = cur.fetchone()
            if not user:
                return jsonify({"success": False, "message": "not_found"}), 404

            plan = normalize_plan(user.get("plan"))
            auth_until = user.get("valid_until") or default_auth_until_ms()

            # persist defaults
            cur.execute(
                """
                UPDATE users
                SET plan=%s,
                    valid_until=COALESCE(valid_until, %s)
                WHERE lower(email)=lower(%s)
                """,
                (plan, auth_until, email)
            )

            # ensure usage row exists
            cur.execute(
                """
                INSERT INTO usage (user_email, month, used, used_ids)
                VALUES (%s, %s, 0, '[]'::jsonb)
                ON CONFLICT (user_email, month) DO NOTHING
                """,
                (email, month)
            )

            # stellt sicher, dass alte/null Werte repariert werden
            cur.execute("""
                UPDATE usage
                SET used = COALESCE(used, 0),
                    used_ids = COALESCE(used_ids, '[]'::jsonb)
                WHERE user_email=%s AND month=%s
            """, (email, month))
            
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
