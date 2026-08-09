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

# Branding
INLINE_IMAGE_URL = "https://cdn.prod.website-files.com/6708fb5e3fc8d4e5e1c21d6c/69a37a7078f60a1070a61734_RT%20Portrait.JPEG"
SIGNATURE_IMAGE_URL = "https://cdn.prod.website-files.com/6708fb5e3fc8d4e5e1c21d6c/69a37a3f4a7f3504c93be36e_Digital%20Sign%20RT.png"
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

        mailtext = f"""
Neue Mandatsanfrage:

Name: {name}
Geburtsdatum: {geburtsdatum}
E-Mail: {email}
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
                # Premium-Standardmail (email-safe)
                kunden_subject = "Gratis Vignette! Deine Mandatsanfrage bei TradeSource"
                kunden_text = f"""\
Hallo {name},

Vielen Dank für Dein Vertrauen!

In der Versicherungsberatung entscheidet nicht die schönste Offerte – sondern die Lösung,
die im Alltag und insbesondere im Schadenfall zuverlässig trägt.

Unser Anspruch ist eine Arbeitsweise, die Sie jederzeit nachvollziehen können:
fundierte Empfehlungen, lückenlose Dokumentation und eine Begleitung,
die weit über den Vertragsabschluss hinausreicht.

Wir verbinden persönliche Erreichbarkeit mit strukturierten Prozessen –
damit aus Komplexität Klarheit wird und Sie Ihre Entscheidungen mit Überzeugung treffen können.

Herzlichen Dank für Ihr Vertrauen.

Mit freundlichen Grüssen
Raul Tito
Geschäftsführer
TradeSource Switzerland GmbH
"""
                kunden_msg["Subject"] = kunden_subject

                alt_part = MIMEMultipart("alternative")
                alt_part.attach(MIMEText(kunden_text, "plain", "utf-8"))

                portrait_cid = "raul_portrait"
                sign_cid = "raul_sign"

                html_text = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#060b14;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#060b14;padding:20px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="940" cellspacing="0" cellpadding="0" border="0" style="width:940px;max-width:94%;border:1px solid #233142;border-radius:14px;overflow:hidden;background:#0b1422;">
            <tr>
              <!-- Linke Bildspalte -->
              <td width="36%" valign="top" style="background:#0b1422;border-right:1px solid #1a2638;">
                <img src="cid:{portrait_cid}" alt="Raul Tito" width="100%" style="display:block;width:100%;height:auto;border:0;max-height:560px;object-fit:cover;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#111a28;">
                  <tr>
                    <td style="padding:10px 14px;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.3;color:#ffffff;">
                      <strong style="font-size:15px;">Raul Tito</strong>
                      <span style="color:#a8935e;"> &nbsp;·&nbsp; Geschäftsführer</span>
                      <span style="color:#9ba8ba;"> &nbsp;·&nbsp; ZÜRICH</span>
                    </td>
                  </tr>
                </table>
              </td>

              <!-- Rechte Textspalte -->
              <td width="64%" valign="top" style="padding:26px 28px 22px 28px;font-family:Arial,Helvetica,sans-serif;color:#eaf0f6;">
                <h2 style="margin:0 0 16px 0;font-size:40px;line-height:1.1;color:#ffffff;font-family:Georgia,'Times New Roman',serif;font-weight:700;">Ein persönliches Wort</h2>

                <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:14px;">
                  <tr>
                    <td style="width:4px;background:#a8935e;border-radius:2px;"></td>
                    <td style="padding-left:10px;font-size:38px;line-height:1.2;color:#ffffff;font-family:Georgia,'Times New Roman',serif;font-weight:700;">
                      Sehr geehrte Damen und Herren
                    </td>
                  </tr>
                </table>

                <p style="margin:0 0 14px 0;font-size:31px;line-height:1.72;color:#e6edf5;">
                  In der Versicherungsberatung entscheidet nicht die schönste Offerte – sondern die Lösung,
                  die im Alltag und insbesondere im Schadenfall zuverlässig trägt.
                </p>

                <p style="margin:0 0 14px 0;font-size:31px;line-height:1.72;color:#e6edf5;">
                  Unser Anspruch ist eine Arbeitsweise, die Sie jederzeit nachvollziehen können:
                  fundierte Empfehlungen, lückenlose Dokumentation und eine Begleitung,
                  die weit über den Vertragsabschluss hinausreicht.
                </p>

                <p style="margin:0 0 14px 0;font-size:31px;line-height:1.72;color:#e6edf5;">
                  Wir verbinden persönliche Erreichbarkeit mit strukturierten Prozessen –
                  damit aus Komplexität Klarheit wird und Sie Ihre Entscheidungen mit Überzeugung treffen können.
                </p>

                <p style="margin:0 0 16px 0;font-size:31px;line-height:1.72;color:#ffffff;">
                  Herzlichen Dank für Ihr Vertrauen.
                </p>

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-top:1px solid #1f2d40;padding-top:14px;margin-top:10px;">
                  <tr>
                    <td valign="bottom" style="font-family:Arial,Helvetica,sans-serif;color:#dfe7f0;">
                      <p style="margin:0 0 6px 0;font-size:12px;color:#aeb9c8;">Mit freundlichen Grüssen</p>
                      <p style="margin:0;font-size:28px;font-weight:700;color:#ffffff;">Raul Tito</p>
                      <p style="margin:4px 0 0 0;font-size:11px;color:#93a3b8;">TradeSource Switzerland</p>
                    </td>
                    <td valign="bottom" align="right">
                      <img src="cid:{sign_cid}" alt="Unterschrift Raul Tito" width="150" style="display:block;width:150px;max-width:150px;height:auto;border:0;opacity:.95;">
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>

          <table role="presentation" width="940" cellspacing="0" cellpadding="0" border="0" style="width:940px;max-width:94%;margin-top:10px;">
            <tr>
              <td style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;color:#8ea0b6;padding:0 4px;">
                TradeSource Switzerland GmbH ·
                <a href="tel:+41438830007" style="color:#8ea0b6;text-decoration:none;">043 883 00 07</a> ·
                <a href="tel:+41765720019" style="color:#8ea0b6;text-decoration:none;">076 572 00 19</a> ·
                <a href="mailto:info@tradesource.ch" style="color:#8ea0b6;text-decoration:none;">info@tradesource.ch</a>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
                alt_part.attach(MIMEText(html_text, "html", "utf-8"))
                kunden_msg.attach(alt_part)

                # Portrait laden
                try:
                    with urlopen(INLINE_IMAGE_URL, timeout=10) as resp:
                        img_data = resp.read()
                    portrait = MIMEImage(img_data, _subtype="jpeg")
                    portrait.add_header("Content-ID", f"<{portrait_cid}>")
                    portrait.add_header("Content-Disposition", "inline")
                    kunden_msg.attach(portrait)
                except Exception as img_err:
                    print("Portrait konnte nicht geladen werden:", str(img_err))

                # Signatur laden (optional)
                try:
                    with urlopen(SIGNATURE_IMAGE_URL, timeout=10) as resp:
                        sig_data = resp.read()
                    sign_img = MIMEImage(sig_data, _subtype="png")
                    sign_img.add_header("Content-ID", f"<{sign_cid}>")
                    sign_img.add_header("Content-Disposition", "inline")
                    kunden_msg.attach(sign_img)
                except Exception as sig_err:
                    print("Signaturbild konnte nicht geladen werden:", str(sig_err))

            # PDF optional an Kunden
            if pdf_bytes:
                part = MIMEApplication(pdf_bytes, Name=filename)
                part["Content-Disposition"] = f'attachment; filename="{filename}"'
                kunden_msg.attach(part)

            # V-Card
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
