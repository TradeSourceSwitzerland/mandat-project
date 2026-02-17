import os
import logging
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
    "prod_TxPBWrcKyJ8EiK": "enterprise",  # Live-Preis-ID für Enterprise
    "prod_TxPABMR85vBl2U": "business",    # Live-Preis-ID für Business
    "prod_TxPAEQ2MB1FblT": "basic",       # Live-Preis-ID für Basic
}

# ---------------------------- HELPERS ----------------------------
def normalize_plan(plan: str | None) -> str:
    value = str(plan or "none").strip().lower()
    return value if value in VALID_PLANS else "none"


def normalize_email_candidate(value: str | None) -> str:
    email = str(value or "").strip().lower()
    if not email or " " in email or email.count("@") != 1:
        return ""
    local, domain = email.split("@", 1)
    if not local or not domain or "." not in domain:
        return ""
    return email

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


def resolve_session_id(data: dict) -> str:
    payload_session_id = str(data.get("session_id") or "").strip()
    query_session_id = str(request.args.get("session_id") or "").strip()
    return payload_session_id or query_session_id


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
        os.getenv("STRIPE_PRODUCT_BASIC"): "basic",
        os.getenv("STRIPE_PRODUCT_BUSINESS"): "business",
        os.getenv("STRIPE_PRODUCT_ENTERPRISE"): "enterprise",
    }
    for price_id, plan in env_map.items():
        if price_id:
            mapping[price_id] = plan
    return mapping


def resolve_email_from_checkout_session(checkout_session: dict) -> str:
    email = normalize_email_candidate(checkout_session.get("customer_email"))
    if email:
        return email

    customer_details = checkout_session.get("customer_details") or {}
    email = normalize_email_candidate(customer_details.get("email"))
    if email:
        return email

    metadata = checkout_session.get("metadata") or {}
    for key in ("email", "user_email", "customer_email"):
        email = normalize_email_candidate(metadata.get(key))
        if email:
            return email

    # Fallback: Einige Payment-Links speichern die User-Identität in client_reference_id.
    email = normalize_email_candidate(checkout_session.get("client_reference_id"))
    if email:
        return email

    # Letzter Versuch: E-Mail über Stripe Customer auflösen.
    customer_id = str(checkout_session.get("customer") or "").strip()
    if customer_id:
        try:
            customer = stripe.Customer.retrieve(customer_id)
            email = normalize_email_candidate(customer.get("email"))
            if email:
                return email
        except Exception as exc:
            logging.warning("Stripe customer lookup fehlgeschlagen, customer_id=%s, error=%s", customer_id, exc)

    return ""


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
    line_items = (checkout_session.get("line_items") or {}).get("data")
    if not line_items:
        session_id = checkout_session.get("id")
        if not session_id:
            return "none"

        try:
            line_items = stripe.checkout.Session.list_line_items(session_id, limit=10).get("data", [])
        except Exception as exc:
            logging.warning("Stripe line items konnten nicht geladen werden: %s", exc)
            return "none"

    for item in line_items:
        price = item.get("price") or {}
        price_id = price.get("id")
        product_id = price.get("product")
        resolved = normalize_plan(
            plan_by_price_id.get(price_id) or plan_by_price_id.get(product_id)
        )
        if resolved != "none":
            return resolved

    return "none"


def plan_rank(plan: str) -> int:
    order = {"none": 0, "basic": 1, "business": 2, "enterprise": 3}
    return order.get(normalize_plan(plan), 0)


def resolve_plan_from_subscription(subscription: dict) -> str:
    status = str(subscription.get("status") or "").strip().lower()
    if status not in {"active", "trialing", "past_due"}:
        return "none"

    plan_by_price_id = configured_price_plan_map()
    items = ((subscription.get("items") or {}).get("data") or [])
    best_plan = "none"

    for item in items:
        price = item.get("price") or {}
        price_id = str(price.get("id") or "").strip()
        product_id = str(price.get("product") or "").strip()
        resolved = normalize_plan(plan_by_price_id.get(price_id) or plan_by_price_id.get(product_id))
        if plan_rank(resolved) > plan_rank(best_plan):
            best_plan = resolved

    return best_plan


def sync_user_plan_from_stripe_if_needed(email: str, current_plan: str) -> str:
    normalized_current_plan = normalize_plan(current_plan)

    # Nur heilen, wenn lokal kein verwertbarer Plan vorliegt.
    if normalized_current_plan != "none":
        return normalized_current_plan

    if not stripe.api_key:
        return normalized_current_plan

    try:
        customers = stripe.Customer.list(email=email, limit=5).get("data", [])
    except Exception as exc:
        logging.warning("Stripe Customer-Suche fehlgeschlagen, email=%s, error=%s", email, exc)
        return normalized_current_plan

    best_plan = normalized_current_plan
    for customer in customers:
        customer_id = str(customer.get("id") or "").strip()
        if not customer_id:
            continue

        try:
            subscriptions = stripe.Subscription.list(customer=customer_id, status="all", limit=20).get("data", [])
        except Exception as exc:
            logging.warning("Stripe Subscriptions konnten nicht geladen werden, customer_id=%s, error=%s", customer_id, exc)
            continue

        for subscription in subscriptions:
            resolved = resolve_plan_from_subscription(subscription)
            if plan_rank(resolved) > plan_rank(best_plan):
                best_plan = resolved

    if best_plan != normalized_current_plan:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE users
                        SET plan=%s, valid_until=%s
                        WHERE lower(email)=%s
                        """,
                        (best_plan, default_auth_until_ms(), email),
                    )
                conn.commit()
        except Exception as exc:
            logging.warning("Lokales Plan-Healing fehlgeschlagen, email=%s, error=%s", email, exc)
            return normalized_current_plan

    return best_plan



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

            # Falls Checkout-Sync vorher nicht sauber durchlief, beim Login aus Stripe heilen.
            healed_plan = sync_user_plan_from_stripe_if_needed(email, plan)
            if healed_plan != plan:
                plan = healed_plan
                valid_until = default_auth_until_ms()

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
    session_id = resolve_session_id(data)
    logging.debug("verify-session aufgerufen, session_id=%s", session_id)
    if not session_id:
        return jsonify(success=False, message="missing_session_id"), 400

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id, expand=["line_items.data.price"])
    except Exception as exc:
        logging.warning("Stripe Session konnte nicht geladen werden, session_id=%s, error=%s", session_id, exc)
        return jsonify(success=False, message="invalid_session"), 400

    payment_status = str(checkout_session.get("payment_status") or "").lower()
    session_status = str(checkout_session.get("status") or "").lower()
    logging.debug(
        "Stripe Session geladen, session_id=%s, payment_status=%s, session_status=%s",
        session_id,
        payment_status,
        session_status,
    )
    # Trial-, offene oder kostenfreie Checkout-Sessions sollen nicht in einem Reload-Loop enden.
    # In diesen Fällen darf das Frontend den User direkt ins Dashboard leiten.
    free_or_trial_statuses = {"pending", "unpaid", "no_payment_required"}
    if payment_status in free_or_trial_statuses or session_status == "open":
        logging.info(
            "Session in Testphase/kostenfrei/offen, Dashboard-Redirect erlaubt: session_id=%s, payment_status=%s, session_status=%s",
            session_id,
            payment_status,
            session_status,
        )
        # Für kostenfreie bzw. Trial-Checkouts trotzdem versuchen, den Plan sofort zu synchronisieren.
        updated, message = apply_checkout_result_to_user(checkout_session)
        if not updated:
            logging.warning(
                "Trial-/Free-Session konnte nicht vollständig synchronisiert werden, session_id=%s, message=%s",
                session_id,
                message,
            )
        # Bestehende Frontend-Kompatibilität: Antwort als Erfolg signalisieren.
        return jsonify(success=True, message="in_trial_or_free")

    if payment_status != "paid" and session_status != "complete":
        logging.info(
            "Zahlung nicht abgeschlossen, session_id=%s, payment_status=%s, session_status=%s",
            session_id,
            payment_status,
            session_status,
        )
        return jsonify(success=False, message="payment_not_completed"), 409
    # Session ist abgeschlossen (oder bezahlt): Plan-Update versuchen,
    # aber bei bekannten Race-Conditions nicht in einen Reload-Loop zwingen.
    updated, message = apply_checkout_result_to_user(checkout_session)
    if not updated:
        logging.warning("User-Update fehlgeschlagen, session_id=%s, message=%s", session_id, message)
        if message in {"missing_customer_email", "user_not_found"}:
            logging.info(
                "Checkout abgeschlossen, aber Sync noch ausstehend; Dashboard-Redirect erlaubt: session_id=%s",
                session_id,
            )
            return jsonify(success=True, message="sync_pending")
            
        status_code = 404 if message == "user_not_found" else 400
        return jsonify(success=False, message=message), status_code

    logging.info("verify-session erfolgreich, session_id=%s", session_id)

    return jsonify(success=True)
