import os
import ssl
import base64
import smtplib
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

app = Flask(__name__)

# 🔧 CORS sauber konfigurieren (alle Origins für /api/*)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

# ----------------------------
# Health-Check (GET + HEAD)  ✅
# ----------------------------
@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "", 200

# ----------------------------
# Konfiguration via Umgebungsvariablen
# ----------------------------
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.ionos.de")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 465))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "info@tradesource.ch")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO", "info@tradesource.ch")

if not EMAIL_HOST_PASSWORD:
    raise RuntimeError("EMAIL_HOST_PASSWORD is not set. Please set the environment variable.")

# ----------------------------
# HTML-Formular anzeigen
# ----------------------------
@app.route("/mandat")
def show_mandat_form():
    return render_template("mandat.html")

# ----------------------------
# API: PDF per Mail versenden
# ----------------------------
@app.route("/api/sendmail", methods=["POST", "OPTIONS"])  # ✅ OPTIONS erlaubt (Preflight)
def sendmail():
    # ✅ Preflight sofort 200 mit CORS-Headern (setzt Flask-CORS)
    if request.method == "OPTIONS":
        return "", 200

    try:
        # defensive: nur JSON akzeptieren
        if not request.is_json:
            return jsonify({"success": False, "error": "Content-Type muss application/json sein."}), 400

        data = request.get_json()
        print("POST /api/sendmail empfangen:", data)

        name = data.get("name", "")
        email = data.get("email", "")
        geburtsdatum = data.get("geburtsdatum", "")
        pdf_base64 = data.get("pdf_base64")
        filename = (data.get("filename") or "mandat.pdf").replace("/", "_").replace("\\", "_")

        mailtext = f"""Neue Mandatsanfrage:

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
                # Falls jemand doch eine Data-URL schickt, abtrennen:
                if "," in pdf_base64:
                    pdf_base64 = pdf_base64.split(",", 1)[1]
                pdf_bytes = base64.b64decode(pdf_base64, validate=True)
                part = MIMEApplication(pdf_bytes, Name=filename)
                part['Content-Disposition'] = f'attachment; filename="{filename}"'
                msg.attach(part)
            except Exception as e:
                print("Fehler beim Dekodieren des PDFs:", str(e))
                return jsonify({"success": False, "error": f"PDF Decode Fehler: {str(e)}"}), 400
        else:
            print("Warnung: Kein PDF im Request enthalten.")

        context = ssl.create_default_context()

        # Admin-Mail
        with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, context=context) as server:
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, EMAIL_TO, msg.as_string())

        print("E-Mail an Admin erfolgreich gesendet ✅")

        # Kunden-Bestätigung (nur wenn E-Mail vorhanden)
        if email:
            kunden_msg = MIMEMultipart()
            kunden_msg["Subject"] = "Gratis Vignette! Deine Mandatsanfrage bei TradeSource"
            kunden_msg["From"] = EMAIL_HOST_USER
            kunden_msg["To"] = email

            kunden_text = f"""Hallo {name},

Vielen Dank für Dein Vertrauen!

Dein Mandat wurde erfolgreich an Deine Versicherung weitergeleitet.

Nächste Schritte:
• Prüfung Deines Mandats durch die Versicherung
• Bei positiver Rückmeldung automatische Aufnahme in unsere Vignetten-Aktion
• Versand Deiner klassischen Autobahn-Vignette im Januar per Post

Wichtige Hinweise:
• Pro Mandat und Kalenderjahr erhältst Du eine pyhische Autobahn-Vignette
• Voraussetzung ist ein aktives und kostenloses Mandatsverhältnis via TradeSource Switzerland GmbH
• Solltest Du eine E-Vignette wünschen, fordere diese bitte separat per E-Mail an: info@tradesource.ch

Unser Premium-Service ist schweizweit zertifiziert und für Dich garantiert kostenlos. Mit voller Sorgfalt und höchstem Engagement vertreten wir Deine Interessen. Lass Dich daher nicht verunsichern, falls Dein bisheriger Berater versucht, Dich zurückzugewinnen – wir stehen klar, unabhängig und ausschliesslich auf Deiner Seite.

Bei Rückfragen stehen wir Dir jederzeit gerne zur Verfügung.

Mit freundlichen Grüssen
Dein TradeSource-Team

FINMA Nr.: F01452693
Direct: +41 43 883 00 07
E-Mail: info@tradesource.ch
Web: www.tradesource.ch

Transparenz | Fairness | Sicherheit
"""
            kunden_msg.attach(MIMEText(kunden_text, "plain"))

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
        # Wichtig: JSON zurückgeben, damit `response.json()` im Frontend nicht crasht
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
