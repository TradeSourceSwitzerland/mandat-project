from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ZEVIX Blueprint
from zevix import zevix_blueprint
app.register_blueprint(zevix_blueprint)

# Health Check Routes
@app.route('/')
def health_check():
    return 'Service is up!'

@app.route('/healthz')
def health_check_z():
    return jsonify({'status': 'healthy'})

# Email Configuration
import smtplib
from email.mime.text import MIMEText

def send_email(to_address, subject, message):
    msg = MIMEText(message)
    msg['Subject'] = subject
    msg['From'] = 'your-email@example.com'
    msg['To'] = to_address

    with smtplib.SMTP('smtp.example.com') as server:
        server.login('your-email@example.com', 'your-password')
        server.send_message(msg)

# HTML Template Routes
@app.route('/mandat')
def mandat():
    return render_template('mandat.html')

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/leads')
def leads():
    return render_template('leads.html')

# /api/sendmail Endpoint
@app.route('/api/sendmail', methods=['POST'])
def send_mail():
    data = request.json
    to_address = data['to_address']
    subject = data['subject']
    message = data['message']
    send_email(to_address, subject, message)
    return jsonify({'status': 'email sent'})

# Static Files Serving
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

# Main Entry Point
if __name__ == '__main__':
    app.run(debug=True)
