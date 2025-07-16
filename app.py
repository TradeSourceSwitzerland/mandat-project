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
CORS(app)  # 🔥 CORS aktiv für Webflow-Zugriff

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
@app.route("/api/sendmail", methods=["POST"])
def sendmail():
    try:
        data = request.json
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
        msg["Subject"] = "Neue Mandatsformular Anfrage"
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
            kunden_msg["Subject"] = "Deine Mandatsanfrage bei TradeSource"
            kunden_msg["From"] = EMAIL_HOST_USER
            kunden_msg["To"] = email

            kunden_text = f"""\
Hallo {name},

Herzlichen Dank für Dein Vertrauen und Deine Mandatsanfrage bei TradeSource!

Wir freuen uns sehr, dass Du Dich für unseren kostenlosen Service entschieden hast. So profitierst Du nicht nur von den günstigsten Versicherungspreisen – sondern auch von einer völlig unverbindlichen und transparenten Beratung.

Unser Versprechen: Du erhältst garantiert die besten Konditionen am Markt, ohne versteckte Kosten. Lehn Dich entspannt zurück – wir kümmern uns um den Rest!

Wenn Du Fragen oder Wünsche hast, sind wir jederzeit persönlich für Dich da.

TradeSource – Dein Partner für starke Leistungen und den fairsten Preis.

Freundliche Grüße  
Dein TradeSource-Team

Telefon: 043 883 00 07  
E-Mail: info@tradesource.ch  
www.tradesource.ch
"""

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
