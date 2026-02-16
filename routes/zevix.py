import os
import json
import psycopg
from psycopg.rows import dict_row
import bcrypt
import stripe
import jwt
from flask import Flask, Blueprint, jsonify, request, session
from datetime import datetime, timedelta
from hmac import compare_digest

# ---------------------------- CONFIG ----------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your_flask_secret_key")  # Flask Session Secret
DATABASE_URL = os.getenv("DATABASE_URL")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # Dein Stripe-Secret-Key
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")  # Dein geheimer Schlüssel für JWT

VALID_PLANS = {"none", "basic", "business", "enterprise"}

# ---------------------------- HELPERS ----------------------------

def normalize_plan(plan):
    p = str(plan or "none").strip().lower()
    return p if p in VALID_PLANS else "none"

def default_auth_until_ms():
    return int((datetime.now() + timedelta(days=30)).timestamp() * 1000)

def get_month_key():
    return datetime.now().strftime("%Y-%m")

def create_jwt_token(email, plan, valid_until):
    expiration = datetime.utcnow() + timedelta(days=30)  # JWT läuft nach 30 Tagen ab
    payload = {
        "email": email,
        "plan": plan,
        "valid_until": valid_until,
        "exp": expiration,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    return token

def decode_jwt_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def request_payload():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if request.form:
        return request.form.to_dict(flat=True)
    return {}

def find_user_by_email(cur, email):
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

def verify_password(password, stored_password):
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

    # Holen der Plan- und Verbrauchsdaten
    month = get_month_key()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan, valid_until FROM users WHERE email=%s
                """,
                (email,)
            )
            user_data = cur.fetchone()

            plan = user_data["plan"]
            valid_until = user_data["valid_until"] or default_auth_until_ms()

            # Leads-Verbrauch in der gleichen Funktion aktualisieren
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

    # JWT-Token erstellen und in der Session speichern
    token = create_jwt_token(email, plan, valid_until)

    response = jsonify({"success": True, "plan": plan, "valid_until": valid_until, "used": used, "used_ids": used_ids, "token": token})

    # Speichern der Session-Daten in Flask Session (nicht in Cookies)
    session["auth_token"] = token
    session["email"] = email
    session["plan"] = plan
    session["used"] = used
    session["used_ids"] = used_ids

    return response

# ---------------------------- STRIPE WEBHOOK ----------------------------
@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")
    event = None

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, os.getenv("STRIPE_ENDPOINT_SECRET"))
    except ValueError as e:
        return jsonify(success=False), 400
    except stripe.error.SignatureVerificationError as e:
        return jsonify(success=False), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session["customer_email"]
        plan = session["metadata"]["plan"]  # Den Plan aus den Metadaten holen

        # Vorherigen Plan abrufen, um zu prüfen, ob er sich geändert hat
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Vorherigen Plan abrufen
                cur.execute(
                    """
                    SELECT plan FROM users WHERE email=%s
                    """,
                    (email,)
                )
                old_plan = cur.fetchone()["plan"]

                # Wenn der Plan sich geändert hat, den Verbrauch zurücksetzen
                if old_plan != plan:
                    # Leads-Verbrauch zurücksetzen, wenn sich der Plan geändert hat
                    cur.execute(
                        """
                        UPDATE usage
                        SET used = 0, used_ids = '[]'::jsonb
                        WHERE user_email=%s
                        """,
                        (email,)
                    )

                # Plan in der Datenbank aktualisieren
                cur.execute(
                    """
                    UPDATE users
                    SET plan=%s
                    WHERE email=%s
                    """,
                    (plan, email),
                )
                conn.commit()

        # JWT-Token erstellen und zurückgeben
        token = create_jwt_token(email, plan, default_auth_until_ms())

        response = jsonify(success=True)
        response.set_cookie("plan", plan)

        return response

    return jsonify(success=False), 400

# ---------------------------- STARTING THE FLASK APP ----------------------------
if __name__ == "__main__":
    app.register_blueprint(zevix_bp)
    app.run(debug=True)
