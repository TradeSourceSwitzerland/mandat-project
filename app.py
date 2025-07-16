import os
import ssl
import base64
import smtplib
from flask import Flask, request, jsonify, render_template, send_from_directory
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

app = Flask(__name__)

# ----------------------------
# Konfiguration (am besten via .env)
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
# Mail-API für PDF-Versand
# ----------------------------
@app.route("/api/sendmail", methods=["POST"])
def sendmail():
    data = request.json
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
    msg["Subject"] = "Neue Mandatsformular Anfrage"
    msg["From"] = EMAIL_HOST_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(mailtext, "plain"))

    if pdf_base64:
        try:
            pdf_bytes = base64.b64decode(pdf_base64)
            part = MIMEApplication(pdf_bytes, Name=filename)
            part['Content-Disposition'] = f'attachment; filename="{filename}"'
            msg.attach(part)
        except Exception as e:
            return jsonify({"success": False, "error": f"PDF Decode Fehler: {str(e)}"}), 400

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, context=context) as server:
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, EMAIL_TO, msg.as_string())
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ----------------------------
# Optional: Static Datei direkt ausliefern
# ----------------------------
@app.route('/static/<path:filename>')
def custom_static(filename):
    return send_from_directory('static', filename)

# ----------------------------
# Start für lokalen Test
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
