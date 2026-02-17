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

def default_auth_until_ms() -> int:
    return int((datetime.now() + timedelta(days=30)).timestamp() * 1000)

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL fehlt")
    return psycopg.connect(f"{DATABASE_URL}?sslmode=require", row_factory=dict_row)

def resolve_email_from_checkout_session(checkout_session: dict) -> str:
    email = (checkout_session.get("customer_email") or "").strip().lower()
    if email:
        return email
    customer_details = checkout_session.get("customer_details") or {}
    return str(customer_details.get("email") or "").strip().lower()

def resolve_plan_from_checkout_session(checkout_session: dict) -> str:
    plan_by_price_id = {
        "prod_TxPBWrcKyJ8EiK": "enterprise",
        "prod_TxPABMR85vBl2U": "business",
        "prod_TxPAEQ2MB1FblT": "basic",
    }
    
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
        resolved = normalize_plan(plan_by_price_id.get(price_id) or plan_by_price_id.get(product_id))
        if resolved != "none":
            return resolved

    return "none"

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

# ---------------------------- Blueprint für ZEVIX ----------------------------
zevix_bp = Blueprint("zevix", __name__)

# ---------------------------- GET Abo-Status ----------------------------
@zevix_bp.route("/api/get_subscription_status", methods=["GET"])
def get_subscription_status():
    # session_id aus der URL holen
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"success": False, "message": "missing_session_id"}), 400

    try:
        # Stripe-Session anhand der session_id abrufen
        checkout_session = stripe.checkout.Session.retrieve(session_id, expand=["line_items.data.price"])
    except Exception as exc:
        logging.warning("Stripe Session konnte nicht geladen werden, session_id=%s, error=%s", session_id, exc)
        return jsonify({"success": False, "message": "invalid_session"}), 400

    # Hier wird der Plan aus der Stripe-Session extrahiert
    plan = resolve_plan_from_checkout_session(checkout_session)

    # Optional: Ablaufdatum aus der Session-Daten oder Default-Wert
    valid_until = default_auth_until_ms()

    # Benutzerinformationen aus der Datenbank abrufen
    email = resolve_email_from_checkout_session(checkout_session)
    if not email:
        return jsonify({"success": False, "message": "missing_customer_email"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Holen des Plan- und Ablaufdatums des Benutzers
            cur.execute(
                """
                SELECT plan, valid_until
                FROM users
                WHERE lower(email)=%s
                """,
                (email.lower(),),
            )
            user_data = cur.fetchone()
            if not user_data:
                return jsonify({"success": False, "message": "user_not_found"}), 404

            # Benutzerplan und Ablaufdatum
            user_plan = normalize_plan(user_data.get("plan"))
            user_valid_until = int(user_data.get("valid_until") or default_auth_until_ms())

    return jsonify({
        "success": True,
        "plan": user_plan,
        "valid_until": user_valid_until,
        "auth_until": user_valid_until,  # Optional, für die Frontend-Kompatibilität
    })
