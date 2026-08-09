import os
import ssl
import base64
import logging
import smtplib
from urllib.request import urlopen
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.utils import formataddr

# ZEVIX Route laden
from routes.zevix import zevix_bp

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your_flask_secret_key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

CORS(
    app,
    supports_credentials=True,
    origins=[
        "https://www.zevix.ch",
        "https://zevix.ch",
        "https://zevix.webflow.io",
        "https://www.tradesource.ch",
        "https://tradesource.ch",
    ],
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "OPTIONS"],
)

app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_DOMAIN=None,
)

app.register_blueprint(zevix_bp)


@app.before_request
def log_request():
    logging.info("Incoming request: %s %s from %s", request.method, request.path, request.remote_addr)


@app.route("/zevix/login", methods=["OPTIONS"])
def login_options():
    response = jsonify({"status": "ok"})
    response.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin"))
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "POST,OPTIONS")
    response.headers.add("Access-Control-Allow-Credentials", "true")
    return response, 200


@app.route("/healthz", methods=["HEAD"])
def healthz():
    return "", 200


EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.ionos.de")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "465").strip())
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "info@tradesource.ch")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO", "info@tradesource.ch")

INLINE_IMAGE_URL = "https://cdn.prod.website-files.com/6708fb5e3fc8d4e5e1c21d6c/69a37a7078f60a1070a61734_RT%20Portrait.JPEG"
SENDER_DISPLAY_NAME = "Raul Tito | TradeSource Switzerland GmbH"


@app.route("/mandat")
def show_mandat_form():
    return render_template("mandat.html")


@app.route("/login")
def show_login():
    return render_template("login.html")


@app.route("/dashboard")
def show_dashboard():
    return render_template("dashboard.html")


@app.route("/leads")
def show_leads():
    return render_template("leads.html")


@app.route("/api/sendmail", methods=["POST"])
def sendmail():
    if not EMAIL_HOST_PASSWORD:
        return jsonify({"success": False, "error": "Mail configuration missing"}), 500

    try:
        data = request.json or {}
        form_source = data.get("form_source", "mandat_original")
        print("POST /api/sendmail empfangen:", data)

        name = data.get("name", "")
        email = data.get("email", "")
        geburtsdatum = data.get("geburtsdatum", "")
        pdf_base64 = data.get("pdf_base64")
        filename = data.get("filename", "mandat.pdf")
        versicherung_name = data.get("versicherung_name", "").strip()

        mailtext = f"""
Neue Mandatsanfrage:

Name: {name}
Geburtsdatum: {geburtsdatum}
E-Mail: {email}
Versicherung: {versicherung_name or "-"}
"""

        # -------- Admin-Mail --------
        msg = MIMEMultipart()
        msg["Subject"] = f"{name}, Neue Mandatsformular Anfrage"
        msg["From"] = EMAIL_HOST_USER
        msg["To"] = EMAIL_TO
        msg.attach(MIMEText(mailtext, "plain", "utf-8"))

        pdf_bytes = None
        if pdf_base64:
            try:
                pdf_bytes = base64.b64decode(pdf_base64)
                part = MIMEApplication(pdf_bytes, Name=filename)
                part["Content-Disposition"] = f'attachment; filename="{filename}"'
                msg.attach(part)
            except Exception as e:
                print("Fehler beim Dekodieren des PDFs:", str(e))
                return jsonify({"success": False, "error": f"PDF Decode Fehler: {str(e)}"}), 400
        else:
            print("Warnung: Kein PDF im Request enthalten.")

        context = ssl.create_default_context()

        with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, context=context) as server:
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, EMAIL_TO, msg.as_string())

        print("E-Mail an Admin erfolgreich gesendet ✅")

        # -------- Kundenmail --------
        if email:
            kunden_msg = MIMEMultipart("mixed")
            kunden_msg["From"] = formataddr((SENDER_DISPLAY_NAME, EMAIL_HOST_USER))
            kunden_msg["To"] = email

            # Kontakter bestimmen (unverändert)
            if form_source == "mandat_copy":
                kontakter = "Dardan Bajrami"
            elif form_source == "mandat_jetmir":
                kontakter = "Jetmir"
            else:
                kontakter = None

            if kontakter:
                # Kontakter-Zweig: unverändert plain text
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
                kunden_msg["Subject"] = kunden_subject
                kunden_msg.attach(MIMEText(kunden_text, "plain", "utf-8"))

            else:
                # Standard-Zweig
                kunden_subject = "Gratis Vignette: Bestätigung deiner Mandatsanfrage"
                weiterleitung_text = (
                    f"an {versicherung_name} zur Prüfung weitergeleitet"
                    if versicherung_name
                    else "zur Prüfung weitergeleitet"
                )

                kunden_text = f"""\
Hallo {name},

Vielen Dank für dein Vertrauen und deine Mandatsanfrage.

Wir haben deine Unterlagen erhalten und {weiterleitung_text}.

Falls Informationen fehlen, melden wir uns direkt bei dir.

Freundliche Grüsse
Raul Tito
Geschäftsführer
TradeSource Switzerland GmbH
Partner der INP Finanz GmbH
"""
                kunden_msg["Subject"] = kunden_subject

                image_cid = "raul_portrait"

                html_text = f"""\
<html>
  <body style="margin:0;padding:0;background:#ffffff;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#ffffff;">
      <tr>
        <td align="left" style="padding:24px 20px 14px 20px; font-family:Arial,Helvetica,sans-serif; color:#111111; font-size:16px; line-height:1.6;">
          <p style="margin:0 0 12px 0;">Hallo {name},</p>
          <p style="margin:0 0 12px 0;">Vielen Dank für dein Vertrauen und deine Mandatsanfrage.</p>
          <p style="margin:0 0 18px 0;">
            Wir haben deine Unterlagen erhalten und {weiterleitung_text}.<br>
            Falls Informationen fehlen, melden wir uns direkt bei dir.
          </p>

          <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin-top:8px;">
            <tr>
              <td style="padding:0 14px 0 0; vertical-align:top;">
                <img src="cid:{image_cid}" alt="Raul Tito" width="160"
                     style="display:block; width:160px; max-width:160px; height:auto; border-radius:8px; border:0;">
              </td>
              <td style="vertical-align:top; font-family:Arial,Helvetica,sans-serif; color:#111111; font-size:14px; line-height:1.5;">
                <p style="margin:0 0 8px 0; text-align:left;">Freundliche Grüsse</p>
                <p style="margin:0; font-size:16px; font-weight:700; color:#111111;">Raul Tito</p>
                <p style="margin:2px 0 0 0; color:#444444;">Geschäftsführer</p>
                <p style="margin:6px 0 0 0; color:#111111;">TradeSource Switzerland GmbH</p>
                <p style="margin:2px 0 0 0; color:#666666; font-size:12px;">Partner der INP Finanz GmbH</p>
                <p style="margin:6px 0 0 0; color:#444444;">
                  <a href="tel:+41438830007" style="color:#444444; text-decoration:none;">043 883 00 07</a><br>
                  <a href="tel:+41765720019" style="color:#444444; text-decoration:none;">076 572 00 19</a><br>
                  <a href="mailto:info@tradesource.ch" style="color:#444444; text-decoration:none;">info@tradesource.ch</a>
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

                # MIME-Struktur: mixed > related > alternative
                related_part = MIMEMultipart("related")
                alt_part = MIMEMultipart("alternative")
                alt_part.attach(MIMEText(kunden_text, "plain", "utf-8"))
                alt_part.attach(MIMEText(html_text, "html", "utf-8"))
                related_part.attach(alt_part)

                # Inline-Bild
                try:
                    with urlopen(INLINE_IMAGE_URL, timeout=10) as resp:
                        img_data = resp.read()
                    img = MIMEImage(img_data, _subtype="jpeg")
                    img.add_header("Content-ID", f"<{image_cid}>")
                    img.add_header("Content-Disposition", "inline")
                    related_part.attach(img)
                except Exception as img_err:
                    print("Inline-Bild konnte nicht geladen werden:", str(img_err))

                kunden_msg.attach(related_part)

            # PDF als echter Anhang
            if pdf_bytes:
                part = MIMEApplication(pdf_bytes, Name=filename)
                part["Content-Disposition"] = f'attachment; filename="{filename}"'
                kunden_msg.attach(part)

            # V-Card als echter Anhang
            vcard = """BEGIN:VCARD
VERSION:3.0
N:Tito;Raul;;;
FN:Raul Tito
TITLE:Geschäftsführer
ORG:TradeSource Switzerland GmbH
TEL;TYPE=WORK,VOICE:+41438830007
TEL;TYPE=CELL,VOICE:+41765720019
EMAIL;TYPE=INTERNET:info@tradesource.ch
URL:https://tradesource.ch
END:VCARD
"""
            vcard_part = MIMEText(vcard, _subtype="x-vcard", _charset="utf-8")
            vcard_part.add_header("Content-Disposition", 'attachment; filename="tradesource-kontakt.vcf"')
            kunden_msg.attach(vcard_part)

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


@app.route("/static/<path:filename>")
def custom_static(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
