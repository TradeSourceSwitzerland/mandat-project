import os
import bcrypt
import jwt
import psycopg
import stripe
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session
from hmac import compare_digest
from psycopg.rows import dict_row

# ---------------------------- CONFIG ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")

VALID_PLANS = {"none", "basic", "business", "enterprise"}
DEFAULT_PLAN_BY_PRICE_ID = {
    "price_1Rnw4gD8Uub4ATfM8RBzv8TA": "basic",
    "price_1Rnw5HD8Uub4ATfMRf4jDsiN": "business",
    "price_1Rnw5uD8Uub4ATfMGfYjdWwW": "enterprise",
}

# ---------------------------- HELPERS ----------------------------
def normalize_plan(plan: str | None) -> str:
    value = str(plan or "none").strip().lower()
    return value if value in VALID_PLANS else "none"


def default_auth_until_ms() -> int:
    return int((datetime.now() + timedelta(days=30)).timestamp() * 1000)


def get_month_key() -> str:
    return datetime.now().strftime("%Y-%m")


def create_jwt_token(email: str, plan: str, valid_until: int) -> str:
    expiration = datetime.utcnow() + timedelta(days=30)
    payload = {
        "email": email,
        "plan": normalize_plan(plan),
        "valid_until": valid_until,
        "exp": expiration,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def request_payload() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if request.form:
        return request.form.to_dict(flat=True)
    return {}


def find_user_by_email(cur, email: str) -> dict | None:
    cur.execute(
        """
        SELECT *
        FROM users
        WHERE lower(email)=%s
        ORDER BY id ASC
        LIMIT 1
        """,
        (email.lower(),),
    )
    return cur.fetchone()


def verify_password(password: str, stored_password: str | None) -> bool:
    if not stored_password:
        return False
    candidate = (password or "").encode()
    stored = stored_password.encode()
    try:
        return bcrypt.checkpw(candidate, stored)
    except ValueError:
        return compare_digest(password or "", stored_password)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL fehlt")
    return psycopg.connect(f"{DATABASE_URL}?sslmode=require", row_factory=dict_row)


def configured_price_plan_map() -> dict[str, str]:
    mapping = dict(DEFAULT_PLAN_BY_PRICE_ID)
    env_map = {
        os.getenv("STRIPE_PRICE_BASIC"): "basic",
        os.getenv("STRIPE_PRICE_BUSINESS"): "business",
        os.getenv("STRIPE_PRICE_ENTERPRISE"): "enterprise",
    }
    for price_id, plan in env_map.items():
        if price_id:
            mapping[price_id] = plan
    return mapping


def resolve_email_from_checkout_session(checkout_session: dict) -> str:
    email = (checkout_session.get("customer_email") or "").strip().lower()
    if email:
        return email
    customer_details = checkout_session.get("customer_details") or {}
    return str(customer_details.get("email") or "").strip().lower()


def apply_checkout_result_to_user(checkout_session: dict) -> tuple[bool, str]:
    email = resolve_email_from_checkout_session(checkout_session)
    if not email:
        return False, "missing_customer_email"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT plan FROM users WHERE lower(email)=%s", (email,))
            user_row = cur.fetchone()
            if not user_row:
                return False, "user_not_found"

            old_plan = normalize_plan(user_row.get("plan"))
            new_plan = resolve_plan_from_checkout_session(checkout_session)

            # Niemals auf none downgraden, wenn keine belastbare Plan-Info vorliegt
            if new_plan == "none":
                new_plan = old_plan

            if old_plan != new_plan:
                cur.execute(
                    """
                    UPDATE usage
                    SET used = 0, used_ids = '[]'::jsonb
                    WHERE user_email=%s
                    """,
                    (email,),
                )

            cur.execute(
                """
                UPDATE users
                SET plan=%s, valid_until=%s
                WHERE lower(email)=%s
                """,
                (new_plan, default_auth_until_ms(), email),
            )
        conn.commit()

    return True, "ok"


def resolve_plan_from_checkout_session(checkout_session: dict) -> str:
    # 1) bevorzugt metadata.plan
    metadata_plan = normalize_plan((checkout_session.get("metadata") or {}).get("plan"))
    if metadata_plan != "none":
        return metadata_plan

    # 2) fallback via Stripe Price-ID (robuster für Payment Links)
    plan_by_price_id = configured_price_plan_map()
    session_id = checkout_session.get("id")
    if not session_id:
        return "none"

    try:
        line_items = stripe.checkout.Session.list_line_items(session_id, limit=10)
    except Exception:
        return "none"

    for item in line_items.get("data", []):
        price = item.get("price") or {}
        price_id = price.get("id")
        resolved = normalize_plan(plan_by_price_id.get(price_id))
        if resolved != "none":
            return resolved

    return "none"


# ---------------------------- Blueprint für ZEVIX ----------------------------
zevix_bp = Blueprint("zevix", __name__)

# ---------------------------- REGISTER ----------------------------
@zevix_bp.route("/zevix/register", methods=["POST"])
def register():
    data = request_payload()
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


# ---------------------------- LOGIN ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def login():
    data = request_payload()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "missing"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            user = find_user_by_email(cur, email)
            if not user:
                return jsonify({"success": False, "message": "not_found"}), 404
            if not verify_password(password, user.get("password")):
                return jsonify({"success": False, "message": "wrong_password"}), 401

    month = get_month_key()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan, valid_until
                FROM users
                WHERE lower(email)=%s
                """,
                (email,),
            )
            user_data = cur.fetchone() or {}

            plan = normalize_plan(user_data.get("plan"))
            valid_until = int(user_data.get("valid_until") or default_auth_until_ms())

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

    token = create_jwt_token(email, plan, valid_until)

    response = jsonify(
        {
            "success": True,
            "email": email,
            "plan": plan,
            "valid_until": valid_until,
            # Frontend-Kompatibilität (bestehendes Webflow-Script erwartet auth_until)
            "auth_until": valid_until,
            "month": month,
            "used": used,
            "used_ids": used_ids,
            "token": token,
        }
    )

    session["auth_token"] = token
    session["email"] = email
    session["plan"] = plan
    session["used"] = used
    session["used_ids"] = used_ids

    return response


# ---------------------------- VERIFY SESSION ----------------------------
@zevix_bp.route("/zevix/verify-session", methods=["POST"])
def verify_session():
    data = request_payload()
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        return jsonify(success=False, message="missing_session_id"), 400

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return jsonify(success=False, message="invalid_session"), 400

    payment_status = str(checkout_session.get("payment_status") or "").lower()
    session_status = str(checkout_session.get("status") or "").lower()
    if payment_status != "paid" and session_status != "complete":
        return jsonify(success=False, message="payment_not_completed"), 409

    updated, message = apply_checkout_result_to_user(checkout_session)
    if not updated:
        status_code = 404 if message == "user_not_found" else 400
        return jsonify(success=False, message=message), status_code

    return jsonify(success=True)


# ---------------------------- STRIPE WEBHOOK ----------------------------
@zevix_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, os.getenv("STRIPE_ENDPOINT_SECRET"))
    except ValueError:
        return jsonify(success=False, error="invalid_payload"), 400
    except stripe.error.SignatureVerificationError:
        return jsonify(success=False, error="invalid_signature"), 400

    if event.get("type") != "checkout.session.completed":
        return jsonify(success=True)

    checkout_session = event.get("data", {}).get("object", {})
    updated, message = apply_checkout_result_to_user(checkout_session)
    if not updated:
        status_code = 404 if message == "user_not_found" else 400
        return jsonify(success=False, error=message), status_code

    return jsonify(success=True)
