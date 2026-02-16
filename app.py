import os
import ssl
import base64
import smtplib
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

# ZEVIX Route laden
from routes.zevix import zevix_bp

app = Flask(__name__)
# HTTPS-Information von Reverse-Proxies (z. B. Render) korrekt übernehmen
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Cookies über Cross-Site Requests erlauben (Webflow -> Backend)
CORS(app, supports_credentials=True)
# ZEVIX Blueprint registrieren
app.register_blueprint(zevix_bp)
# ----------------------------
# Health‑Check für Wake‑Up Pings
# ----------------------------
@app.route("/healthz", methods=["HEAD"])
def healthz():
    return "", 200
# ----------------------------
# Konfiguration via Umgebungsvariablen
# ----------------------------
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.ionos.de")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "465").strip())
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "info@tradesource.ch")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO", "info@tradesource.ch")


# ----------------------------
# HTML-Formular anzeigen
# ----------------------------
@app.route("/mandat")
def show_mandat_form():
    return render_template("mandat.html")

# ----------------------------
# API: PDF per Mail versenden
# ----------------------------
@app.route("/api/sendmail", methods=["POST"])
def sendmail():
    # Config erst zur Laufzeit prüfen (nicht beim App-Start!)
    if not EMAIL_HOST_PASSWORD:
        return jsonify({
            "success": False,
            "error": "Mail configuration missing"
        }), 500

    try:
        data = request.json
        form_source = data.get("form_source", "mandat_original")
        print("POST /api/sendmail empfangen:", data)

        name = data.get("name", "")
        email = data.get("email", "")
        geburtsdatum = data.get("geburtsdatum", "")
        pdf_base64 = data.get("pdf_base64", None)
        filename = data.get("filename", "mandat.pdf")

        mailtext = f"""
Neue Mandatsanfrage:

Name: {name}
Geburtsdatum: {geburtsdatum}
E-Mail: {email}
"""

        msg = MIMEMultipart()
        msg["Subject"] = f"{name}, Neue Mandatsformular Anfrage"
        msg["From"] = EMAIL_HOST_USER
        msg["To"] = EMAIL_TO
        msg.attach(MIMEText(mailtext, "plain"))

        pdf_bytes = None
        if pdf_base64:
            try:
                pdf_bytes = base64.b64decode(pdf_base64)
                part = MIMEApplication(pdf_bytes, Name=filename)
                part['Content-Disposition'] = f'attachment; filename="{filename}"'
                msg.attach(part)
            except Exception as e:
                print("Fehler beim Dekodieren des PDFs:", str(e))
                return jsonify({"success": False, "error": f"PDF Decode Fehler: {str(e)}"}), 400
        else:
            print("Warnung: Kein PDF im Request enthalten.")

        context = ssl.create_default_context()
        # Sende interne Mail an Admin
        with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, context=context) as server:
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, EMAIL_TO, msg.as_string())

        print("E-Mail an Admin erfolgreich gesendet ✅")

        # --------
        # Sende Bestätigungsmail an den Kunden
        # --------
        if email:
            kunden_msg = MIMEMultipart()
            kunden_msg["From"] = EMAIL_HOST_USER
            kunden_msg["To"] = email

            # Kontakter bestimmen
            if form_source == "mandat_copy":
                kontakter = "Dardan Bajrami"
            elif form_source == "mandat_jetmir":
                kontakter = "Jetmir"
            else:
                kontakter = None

            # Kundenmail-Text
            if kontakter:
                kunden_subject = "Bestätigung: Mandat erfolgreich eingereicht"
                kunden_text = f"""\
Hallo {name},

hiermit bestätigen wir den Eingang deines Mandats.

Das Mandat wurde erfolgreich durch unseren Kontakter  
{kontakter} bei TradeSource Switzerland GmbH eingereicht.

Bei Rückfragen stehen wir dir jederzeit gerne zur Verfügung.

Freundliche Grüsse  
TradeSource Switzerland GmbH
"""
            else:
                kunden_subject = "Gratis Vignette! Deine Mandatsanfrage bei TradeSource"
                kunden_text = f"""\
Hallo {name},

Vielen Dank für Dein Vertrauen!

Deine Anfrage wurde erfolgreich an Deine Versicherung weitergeleitet.

Mit freundlichen Grüssen  
Dein TradeSource-Team
"""

            kunden_msg["Subject"] = kunden_subject
            kunden_msg.attach(MIMEText(kunden_text, "plain"))

            # Optional: PDF auch an den Kunden anhängen
            if pdf_bytes:
                part = MIMEApplication(pdf_bytes, Name=filename)
                part['Content-Disposition'] = f'attachment; filename="{filename}"'
                kunden_msg.attach(part)

            with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, context=context) as server:
                server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
                server.sendmail(EMAIL_HOST_USER, email, kunden_msg.as_string())

            print("Bestätigungsmail an Kunde erfolgreich gesendet ✅")
        else:
            print("Keine Kunden-E-Mail angegeben, Bestätigungsmail wird nicht versendet.")



        return jsonify({"success": True})

    except Exception as e:
        print("Fehler in /api/sendmail:", str(e))
        return jsonify({"success": False, "error": str(e)}), 500

# ----------------------------
# Optional: Static Datei direkt ausliefern
# ----------------------------
@app.route('/static/<path:filename>')
def custom_static(filename):
    return send_from_directory('static', filename)

# ----------------------------
# Lokaler Start
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
