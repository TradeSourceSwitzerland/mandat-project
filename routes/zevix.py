 import os
 import json
 import psycopg
 from psycopg.rows import dict_row
 import bcrypt
 import stripe
 import jwt
 from flask import Blueprint, jsonify, request, session
 from datetime import datetime, timedelta
 from hmac import compare_digest
 
 # ---------------------------- CONFIG ----------------------------
 DATABASE_URL = os.getenv("DATABASE_URL")
 stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # Dein Stripe-Secret-Key
 SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")  # Dein geheimer Schlüssel für JWT
 
 VALID_PLANS = {"none", "basic", "business", "enterprise"}
 
 # ---------------------------- HELPERS ----------------------------
 
 def normalize_plan(plan):
     p = str(plan or "none").strip().lower()
     return p if p in VALID_PLANS else "none"
 
 def default_auth_until_ms():
     return int((datetime.now()   timedelta(days=30)).timestamp() * 1000)
 
 def get_month_key():
     return datetime.now().strftime("%Y-%m")
 
 def create_jwt_token(email, plan, valid_until):
     expiration = datetime.utcnow()   timedelta(days=30)  # JWT läuft nach 30 Tagen ab
     payload = {
         "email": email,
         "plan": plan,
         "valid_until": valid_until,
         "exp": expiration,
@@ -169,92  167,100 @@ def login():
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
 @zevix_bp.route("/webhook", methods=["POST"])
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
         checkout_session = event["data"]["object"]
         email = (checkout_session.get("customer_email") or "").strip().lower()
         plan = normalize_plan((checkout_session.get("metadata") or {}).get("plan"))  # Den Plan aus den Metadaten holen
 
         if not email:
             return jsonify(success=False, error="missing_customer_email"), 400
 
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
                 old_user = cur.fetchone()
                 old_plan = normalize_plan((old_user or {}).get("plan"))
 
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
     from flask import Flask
 
     app = Flask(__name__)
     app.secret_key = os.getenv("FLASK_SECRET_KEY", "your_flask_secret_key")
     app.register_blueprint(zevix_bp)
     app.run(debug=True)
