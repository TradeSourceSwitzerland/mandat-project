# app.py

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import logging
from your_blueprint import zevix_blueprint

# Flask configuration
app = Flask(__name__)
CORS(app)

# Logging configuration
logging.basicConfig(level=logging.INFO)

# Register ZEVIX blueprint
app.register_blueprint(zevix_blueprint)

# Health check routes
@app.route('/', methods=['GET'])
def health_check():
    return 'Service is up!'

@app.route('/healthz', methods=['GET'])
def health_check_z():
    return jsonify(status='healthy'), 200

# Email configuration
MAIL_SERVER = 'smtp.yourserver.com'
MAIL_PORT = 587
MAIL_USE_TLS = True
MAIL_USERNAME = 'your_email@yourserver.com'
MAIL_PASSWORD = 'your_password'

# HTML routes
@app.route('/mandat', methods=['GET'])
def mandat():
    return render_template('mandat.html')

@app.route('/login', methods=['GET'])
def login():
    return render_template('login.html')

@app.route('/dashboard', methods=['GET'])
def dashboard():
    return render_template('dashboard.html')

@app.route('/leads', methods=['GET'])
def leads():
    return render_template('leads.html')

# Email sending logic
@app.route('/api/sendmail', methods=['POST'])
def send_email():
    data = request.json
    # Implement email sending logic here
    # Admin and customer confirmation emails
    return jsonify({'message': 'Emails sent successfully!'}), 200

# Static file serving route
@app.route('/static/<path:path>', methods=['GET'])
def send_static(path):
    return send_from_directory('static', path)

# Main entry point
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
