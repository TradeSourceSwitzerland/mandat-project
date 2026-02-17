import os
import stripe
import psycopg
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request

# ---------------------------- CONFIG ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# ---------------------------- HELPERS ----------------------------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL fehlt")
    return psycopg.connect(f"{DATABASE_URL}?sslmode=require")

def resolve_plan_from_checkout_session(checkout_session: dict) -> str:
    plan_by_price_id = {
        "prod_TxPBWrcKyJ8EiK": "enterprise",
        "prod_TxPABMR85vBl2U": "business",
        "prod_TxPAEQ2MB1FblT": "basic",
    }
    
    line_items = (checkout_session.get("line_items") or {}).get("data")
    for item in line_items:
        price = item.get("price") or {}
        price_id = price.get("id")
        product_id = price.get("product")
        plan = plan_by_price_id.get(price_id) or plan_by_price_id.get(product_id)
        if plan:
            return plan
    return "none"

def resolve_email_from_checkout_session(checkout_session: dict) -> str:
    email = (checkout_session.get("customer_email") or "").strip().lower()
    if email:
        return email
    customer_details = checkout_session.get("customer_details") or {}
    return str(customer_details.get("email") or "").strip().lower()

def default_auth_until_ms() -> int:
    return int((datetime.now() + timedelta(days=30)).timestamp() * 1000)

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
        return jsonify({"success": False, "message": "invalid_session"}), 400

    # Plan aus der Stripe-Session extrahieren
    plan = resolve_plan_from_checkout_session(checkout_session)

    # Benutzer-E-Mail aus der Stripe-Session extrahieren
    email = resolve_email_from_checkout_session(checkout_session)
    if not email:
        return jsonify({"success": False, "message": "missing_customer_email"}), 400

    # Benutzerinformationen aus der Datenbank abrufen
    with get_conn() as conn:
        with conn.cursor() as cur:
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
            user_plan = user_data.get("plan", "none")
            user_valid_until = int(user_data.get("valid_until", default_auth_until_ms()))

    return jsonify({
        "success": True,
        "plan": user_plan,
        "valid_until": user_valid_until,
        "auth_until": user_valid_until,  # Optional für Frontend-Kompatibilität
    })
