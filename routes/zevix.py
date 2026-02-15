import os
import psycopg
from psycopg.rows import dict_row
import bcrypt
from flask import Blueprint, jsonify, request
from datetime import datetime
import pandas as pd
import tempfile

# ----------------------------
# DATABASE CONNECTION
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    # Render Postgres braucht sslmode=require
    return psycopg.connect(f"{DATABASE_URL}?sslmode=require", row_factory=dict_row)

# ----------------------------
# INITIALIZE DATABASE (create table if missing)
# ----------------------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # USERS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            plan TEXT DEFAULT 'basic'
        );
    """)

    # USAGE (zählt verbrauchte Leads pro Monat)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            month TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            UNIQUE(user_email, month)
        );
    """)

    conn.commit()
    cur.close()
    conn.close()

# Tabelle beim Start sicherstellen
init_db()

# ----------------------------
# BLUEPRINT
# ----------------------------
zevix_bp = Blueprint("zevix", __name__)

# ----------------------------
# HEALTH CHECK
# ----------------------------
@zevix_bp.route("/zevix/health", methods=["GET"])
def zevix_health():
    return jsonify({"status": "ZEVIX backend running"})

# ----------------------------
# REGISTER USER
# ----------------------------
@zevix_bp.route("/zevix/register", methods=["POST"])
def register():
    data = request.get_json()

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"success": False, "message": "Missing email or password"}), 400

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            "INSERT INTO users (email, password) VALUES (%s, %s)",
            (email, hashed.decode("utf-8"))
        )
        conn.commit()
    except Exception:
        return jsonify({"success": False, "message": "User already exists"}), 400
    finally:
        cur.close()
        conn.close()

    return jsonify({"success": True})

# ----------------------------
# LOGIN USER
# ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def zevix_login():
    data = request.get_json()

    email = data.get("email")
    password = data.get("password")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = cur.fetchone()

    cur.close()
    conn.close()

    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    stored_password = user["password"]

    # psycopg kann memoryview zurückgeben → konvertieren
    if isinstance(stored_password, memoryview):
        stored_password = stored_password.tobytes()
    elif isinstance(stored_password, str):
        stored_password = stored_password.encode("utf-8")

    if not bcrypt.checkpw(password.encode("utf-8"), stored_password):
        return jsonify({"success": False, "message": "Wrong password"}), 401

    return jsonify({
        "success": True,
        "user": {"email": email}
    })
