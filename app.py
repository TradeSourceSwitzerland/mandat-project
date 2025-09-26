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
# CORS für /api/* erlauben, inkl. Preflight
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

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
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "465"))          # Default 465
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "info@tradesource.ch")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO", "info@tradesource.ch")
EMAIL_SMTP_MODE = os.getenv("EMAIL_SMTP_MODE", "SSL").upper()  # Default SSL

if not EMAIL_HOST_PASSWORD:
    raise RuntimeError("EMAIL_HOST_PASSWORD is not set.")

# ----------------------------
# Utility: Senden (SSL/STARTTLS)
# ----------------------------
def _send_via_starttls(msg_str: str, to_addr: str, port: int = 587):
    context = ssl.create_default_context()
    with smtplib.SMTP(EMAIL_HOST, port, timeout=SMTP_TIMEOUT) as server:
        if EMAIL_DEBUG:
            server.set_debuglevel(1)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
        server.sendmail(EMAIL_HOST_USER, to_addr, msg_str)

def _send_via_ssl(msg_str: str, to_addr: str, port: int = 465):
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(EMAIL_HOST, port, context=context, timeout=SMTP_TIMEOUT) as server:
        if EMAIL_DEBUG:
            server.set_debuglevel(1)
        server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
        server.sendmail(EMAIL_HOST_USER, to_addr, msg_str)

def send_mail_with_fallback(msg_str: str, to_addr: str):
    """
    Standard: SSL@EMAIL_PORT (Default 465).
    STARTTLS@587 nur, wenn EMAIL_SMTP_MODE=STARTTLS.
    AUTO: erst SSL@465, dann STARTTLS@587.
    """
    try_order = []

    if EMAIL_SMTP_MODE == "SSL":
        try_order = [("SSL", EMAIL_PORT or 465)]
    elif EMAIL_SMTP_MODE == "STARTTLS":
        try_order = [("STARTTLS", EMAIL_PORT or 587)]
    else:  # AUTO
        try_order = [("SSL", 465), ("STARTTLS", 587)]

    errors = []
    for mode, port in try_order:
        try:
            if mode == "STARTTLS":
                _send_via_starttls(msg_str, to_addr, int(port))
            else:
                _send_via_ssl(msg_str, to_addr, int(port))
            return True, None
        except smtplib.SMTPAuthenticationError as e:
            return False, f"SMTP Auth fehlgeschlagen: {e}"
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
                socket.timeout, OSError, smtplib.SMTPException) as e:
            errors.append(f"{mode}@{EMAIL_HOST}:{port} -> {e}")

    return False, "; ".join(errors) if errors else "Unbekannter SMTP-Fehler"

# ----------------------------
# HTML-Formular anzeigen
# ----------------------------
@app.route("/mandat")
def show_mandat_form():
    return render_template("mandat.html")

# ----------------------------
# API: PDF per Mail versenden
# ----------------------------
@app.route("/api/sendmail", methods=["POST", "OPTIONS"])
def sendmail():
    # Preflight
    if request.method == "OPTIONS":
        return "", 200

    try:
        if not request.is_json:
            return jsonify({"success": False, "error": "Content-Type muss application/json sein."}), 400

        data = request.get_json(force=True, silent=True) or {}
        print("POST /api/sendmail empfangen:", {
            k: (v[:50] + "...") if isinstance(v, str) and len(v) > 60 else v
            for k, v in data.items()
        })

        name = data.get("name", "").strip()
        email = data.get("email", "").strip()
        geburtsdatum = data.get("geburtsdatum", "").strip()
        pdf_base64 = data.get("pdf_base64")
        filename = (data.get("filename") or "mandat.pdf").replace("/", "_").replace("\\", "_")

        # Mailtext
        mailtext = f"""Neue Mandatsanfrage:

Name: {name}
Geburtsdatum: {geburtsdatum}
E-Mail: {email}
"""

        # Admin-Mail vorbereiten
        admin_msg = MIMEMultipart()
        admin_msg["Subject"] = f"{name or 'Unbekannt'}, Neue Mandatsformular Anfrage"
        admin_msg["From"] = EMAIL_HOST_USER
        admin_msg["To"] = EMAIL_TO
        admin_msg.attach(MIMEText(mailtext, "plain"))

        pdf_bytes = None
        if pdf_base64:
            try:
                # Data-URL support
                if isinstance(pdf_base64, str) and "," in pdf_base64:
                    pdf_base64 = pdf_base64.split(",", 1)[1]
                pdf_bytes = base64.b64decode(pdf_base64, validate=True)
                part = MIMEApplication(pdf_bytes, Name=filename)
                part['Content-Disposition'] = f'attachment; filename="{filename}"'
                admin_msg.attach(part)
            except Exception as e:
                print("Fehler beim Dekodieren des PDFs:", str(e))
                return jsonify({"success": False, "error": f"PDF Decode Fehler: {str(e)}"}), 400
        else:
            print("Warnung: Kein PDF im Request enthalten.")

        # Senden an Admin
        ok, err = send_mail_with_fallback(admin_msg.as_string(), EMAIL_TO)
        if not ok:
            return jsonify({"success": False, "error": f"SMTP Fehler (Admin): {err}"}), 502

        print("E-Mail an Admin erfolgreich gesendet ✅")

        # Bestätigung an Kunden (optional)
        if email:
            kunden_msg = MIMEMultipart()
            kunden_msg["Subject"] = "Gratis Vignette! Deine Mandatsanfrage bei TradeSource"
            kunden_msg["From"] = EMAIL_HOST_USER
            kunden_msg["To"] = email
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
            kunden_msg.attach(MIMEText(kunden_text, "plain"))

            if pdf_bytes:
                part = MIMEApplication(pdf_bytes, Name=filename)
                part['Content-Disposition'] = f'attachment; filename="{filename}"'
                kunden_msg.attach(part)

            ok, err = send_mail_with_fallback(kunden_msg.as_string(), email)
            if not ok:
                # Admin ok, Kunde fehlgeschlagen -> success mit Warnung
                return jsonify({"success": True, "warning": f"Admin ok, Bestätigung an Kunde fehlgeschlagen: {err}"}), 200

            print("Bestätigungsmail an Kunde erfolgreich gesendet ✅")
        else:
            print("Keine Kunden-E-Mail angegeben, Bestätigungsmail wird nicht versendet.")

        return jsonify({"success": True}), 200

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
    # Flask Dev-Server (in Prod hinter Gunicorn)
    app.run(host="0.0.0.0", port=5000)
