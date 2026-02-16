import os
import json
import psycopg
from psycopg.rows import dict_row
import bcrypt
import stripe
import jwt
from flask import Blueprint, jsonify, request
from datetime import datetime, timedelta

# ----------------------------
# CONFIG
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # Dein Stripe-Secret-Key
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")  # Dein geheimen Schlüssel für JWT

# Cookie-Verhalten über ENV steuerbar, damit Login lokal und in Prod stabil funktioniert.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "auto").strip().lower()
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "auto").strip().lower()

VALID_PLANS = {"none", "basic", "business", "enterprise"}

# ----------------------------
# HELPERS
# ----------------------------
def normalize_plan(plan):
    p = str(plan or "none").strip().lower()
    return p if p in VALID_PLANS else "none"


def default_auth_until_ms():
    return int((datetime.now() + timedelta(days=30)).timestamp() * 1000)


def get_month_key():
    return datetime.now().strftime("%Y-%m")


def create_jwt_token(email):
    expiration = datetime.utcnow() + timedelta(days=30)  # JWT läuft nach 30 Tagen ab
    payload = {
        "email": email,
        "exp": expiration,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    return token


def decode_jwt_token(token):
    return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])

def cookie_options():
    secure = request.is_secure
    if COOKIE_SECURE in {"true", "1", "yes", "on"}:
        secure = True
    elif COOKIE_SECURE in {"false", "0", "no", "off"}:
        secure = False

    if COOKIE_SAMESITE in {"lax", "strict", "none"}:
        samesite = COOKIE_SAMESITE.capitalize()
    else:
        # SameSite=None verlangt Secure=True. Für HTTP-Fälle (lokal/dev) auf Lax fallen.
        samesite = "None" if secure else "Lax"

    return {
        "secure": secure,
        "samesite": samesite,
        "max_age": 30 * 24 * 60 * 60,
    }

def load_user_session(email):
    month = get_month_key()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
            if not user:
                return None

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
                (plan, auth_until, email),
            )

            # Usage row sicherstellen
            cur.execute(
                """
                INSERT INTO usage (user_email, month, used, used_ids)
                VALUES (%s, %s, 0, '[]'::jsonb)
                ON CONFLICT (user_email, month) DO NOTHING
                """,
                (email, month),
            )

            cur.execute(
                """
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
                """,
                (email, month),
            )
            usage = cur.fetchone() or {}
            used = int(usage.get("used") or 0)
            used_ids = usage.get("used_ids") or []

        conn.commit()

    return {
        "email": email,
        "plan": plan,
        "auth_until": auth_until,
        "month": month,
        "used": used,
        "used_ids": used_ids,
    }


# ----------------------------
# DATABASE CONNECTION
# ----------------------------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing")
    return psycopg.connect(f"{DATABASE_URL}?sslmode=require", row_factory=dict_row)


# ----------------------------
# INIT DB (lightweight + migration safe)
# ----------------------------
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # USERS TABLE
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    plan TEXT,
                    valid_until BIGINT
                );
                """
            )

            # USAGE TABLE
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id SERIAL PRIMARY KEY,
                    user_email TEXT NOT NULL,
                    month TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    used_ids JSONB DEFAULT '[]'::jsonb,
                    UNIQUE(user_email, month)
                );
                """
            )

            # Defaults für existierende Nutzer
            cur.execute(
                """
                UPDATE users
                SET plan='none'
                WHERE plan IS NULL OR trim(plan)=''
                """
            )

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
                    (email, hashed, "none", default_auth_until_ms()),
                )
            conn.commit()

        return jsonify({"success": True})

    except psycopg.errors.UniqueViolation:
        return jsonify({"success": False, "message": "exists"}), 400


# ----------------------------
# LOGIN (JWT Token erstellen und zurückgeben)
# ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "missing"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
            if not user:
                return jsonify({"success": False, "message": "not_found"}), 404

            if not bcrypt.checkpw(password.encode(), user["password"].encode()):
                return jsonify({"success": False, "message": "wrong_password"}), 401

    session = load_user_session(email)
    if not session:
        return jsonify({"success": False, "message": "not_found"}), 404

    # JWT Token erstellen
    token = create_jwt_token(email)

    # Erstelle ein Antwortobjekt mit Cookie
    response = jsonify({"success": True, **session, "token": token})

    # JWT als Cookie setzen (HttpOnly und Secure)
    cookie_cfg = cookie_options()

    response.set_cookie(
        "auth_token",
        token,
        httponly=True,
        **cookie_cfg,
    )  # 30 Tage
    # UI-Cookies für bestehende Webflow-Embeds
    response.set_cookie(
        "zevix_email",
        session["email"],
        **cookie_cfg,
    )
    response.set_cookie(
        "plan",
        session["plan"],
        **cookie_cfg,
    )

    return response


@zevix_bp.route("/zevix/session-sync", methods=["POST"])
def session_sync():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    email = (data.get("email") or "").strip().lower()

    if token:
        try:
            payload = decode_jwt_token(token)
            email = (payload.get("email") or "").strip().lower()
        except jwt.PyJWTError:
            return jsonify({"success": False, "message": "invalid_token"}), 401

    if not email:
        return jsonify({"success": False, "message": "missing_identity"}), 400

    session = load_user_session(email)
    if not session:
        return jsonify({"success": False, "message": "not_found"}), 404

    response = jsonify({"success": True, **session})
    cookie_cfg = cookie_options()
    response.set_cookie(
        "zevix_email",
        session["email"],
        **cookie_cfg,
    )
    response.set_cookie(
        "plan",
        session["plan"],
        **cookie_cfg,
    )
    return response


# ----------------------------
# SUCCESS (Stripe Webhook)
# ----------------------------
@zevix_bp.route("/success", methods=["GET"])
def success():
    session_id = request.args.get("session_id")
    plan = request.args.get("plan")

    if not session_id or not plan:
        return jsonify({"error": "Missing session_id or plan"}), 400

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            return jsonify({"error": "Payment not successful"}), 400

        email = session.customer_email
        auth_until = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET plan=%s, valid_until=%s
                    WHERE email=%s
                    """,
                    (plan, auth_until, email),
                )

            conn.commit()

        return (
            jsonify(
                {
                    "message": "Subscription updated successfully",
                    "plan": plan,
                    "auth_until": auth_until,
                }
            ),
            200,
        )

    except stripe.error.StripeError as e:
        return jsonify({"error": f"Stripe error: {str(e)}"}), 500


# ----------------------------
# INIT DB ON IMPORT
# ----------------------------
try:
    print("Initializing ZEVIX DB...")
    init_db()
    print("DB ready")
except Exception as e:
    print("DB INIT FAILED:", e)
