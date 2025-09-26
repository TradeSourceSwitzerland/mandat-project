import os
import ssl
import base64
import smtplib
import socket
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

app = Flask(__name__)
CORS(app)  # CORS für Webflow etc.

# ----------------------------
# Health-Check
# ----------------------------
@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "", 200

# ----------------------------
# Konfiguration via ENV
# ----------------------------
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.ionos.de")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))  # nur STARTTLS
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "info@tradesource.ch")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO", "info@tradesource.ch")
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "20"))  # Sekunden
EMAIL_DEBUG  = os.getenv("EMAIL_DEBUG", "0") == "1"

if not EMAIL_HOST_PASSWORD:
    raise RuntimeError("EMAIL_HOST_PASSWORD is not set. Please set the environment variable.")

# ----------------------------
# Helper: Mail per STARTTLS (587) mit IPv4-Fallback
# ----------------------------
def send_via_starttls(msg):
    ctx = ssl.create_default_context()

    def _send(open_smtp):
        with open_smtp() as server:
            if EMAIL_DEBUG:
                server.set_debuglevel(1)  # SMTP-Handshake in Logs
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, [msg["To"]], msg.as_string())

    # 1) Standardweg (kann IPv6 erwischen)
    try:
        return _send(lambda: smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=SMTP_TIMEOUT))
    except (smtplib.SMTPConnectError,
            smtplib.SMTPServerDisconnected,
            TimeoutError, socket.timeout, OSError) as e_std:
        # 2) IPv4-Fallback
        try:
            a_records = socket.getaddrinfo(EMAIL_HOST, EMAIL_PORT, socket.AF_INET, socket.SOCK_STREAM)
            ipv4 = a_records[0][4][0]
            def open_ipv4():
                s = smtplib.SMTP(timeout=SMTP_TIMEOUT)  # <- hier auch den ENV-Timeout nutzen
                s.connect(ipv4, EMAIL_PORT)
                s._host = EMAIL_HOST  # wichtig für STARTTLS/SNI
                return s
            return _send(open_ipv4)
        except Exception as e_v4:
            raise e_std from e_v4

# ----------------------------
# HTML-Formular (optional)
# ----------------------------
@app.route("/mandat")
def show_mandat_form():
    return render_template("mandat.html")

# ----------------------------
# API: PDF per Mail versenden
# ----------------------------
@app.route("/api/sendmail", methods=["POST", "OPTIONS"])
def sendmail():
    # CORS Preflight
    if request.method == "OPTIONS":
        return "", 200

    try:
        if not request.is_json:
            return jsonify({"success": False, "error": "Content-Type muss application/json sein."}), 400

        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        geburtsdatum = (data.get("geburtsdatum") or "").strip()
        pdf_base64 = data.get("pdf_base64")
        filename = (data.get("filename") or "mandat.pdf").replace("/", "_").replace("\\", "_")

        # --- Admin-Mail ---
        mailtext = f"""Neue Mandatsanfrage:

Name: {name}
Geburtsdatum: {geburtsdatum}
E-Mail: {email}
"""
        admin_msg = MIMEMultipart()
        admin_msg["Subject"] = f"{name or 'Unbekannt'}, Neue Mandatsformular Anfrage"
        admin_msg["From"] = EMAIL_HOST_USER
        admin_msg["To"] = EMAIL_TO
        admin_msg.attach(MIMEText(mailtext, "plain"))

        pdf_bytes = None
        if pdf_base64:
            try:
                # Falls Data-URL: Präfix abschneiden
                if isinstance(pdf_base64, str) and "," in pdf_base64:
                    pdf_base64 = pdf_base64.split(",", 1)[1]
                pdf_bytes = base64.b64decode(pdf_base64, validate=True)
                part = MIMEApplication(pdf_bytes, Name=filename)
                part['Content-Disposition'] = f'attachment; filename="{filename}"'
                admin_msg.attach(part)
            except Exception as e:
                return jsonify({"success": False, "error": f"PDF Decode Fehler: {e}"}), 400

        # senden
        send_via_starttls(admin_msg)

        # --- Kundenbestätigung (optional) ---
        if email:
            kunden_text = f"""Hallo {name or 'Kunde'},

Vielen Dank für Dein Vertrauen!

Dein Mandat wurde erfolgreich an Deine Versicherung weitergeleitet.

Nächste Schritte:
• Prüfung Deines Mandats durch die Versicherung
• Bei positiver Rückmeldung automatische Aufnahme in unsere Vignetten-Aktion
• Versand Deiner klassischen Autobahn-Vignette im Januar per Post

Wichtige Hinweise:
• Pro Mandat und Kalenderjahr erhältst Du eine physische Autobahn-Vignette
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
            kunden_msg = MIMEMultipart()
            kunden_msg["Subject"] = "Gratis Vignette! Deine Mandatsanfrage bei TradeSource"
            kunden_msg["From"] = EMAIL_HOST_USER
            kunden_msg["To"] = email
            kunden_msg.attach(MIMEText(kunden_text, "plain"))

            if pdf_bytes:
                part = MIMEApplication(pdf_bytes, Name=filename)
                part['Content-Disposition'] = f'attachment; filename="{filename}"'
                kunden_msg.attach(part)

            send_via_starttls(kunden_msg)

        return jsonify({"success": True}), 200

    except smtplib.SMTPAuthenticationError as e:
        return jsonify({"success": False, "error": f"SMTP Auth fehlgeschlagen: {e}"}), 502
    except (smtplib.SMTPException, OSError, TimeoutError, socket.timeout) as e:
        return jsonify({"success": False, "error": f"SMTP Fehler: {e}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ----------------------------
# Static Dateien (optional)
# ----------------------------
@app.route('/static/<path:filename>')
def custom_static(filename):
    return send_from_directory('static', filename)

# ----------------------------
# Lokaler Start (in Prod via Gunicorn)
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
