from flask import Blueprint, jsonify, request

# Blueprint muss GENAU so heißen wie wir ihn importieren!
zevix_bp = Blueprint("zevix", __name__)

# Test-Route
@zevix_bp.route("/zevix/health", methods=["GET"])
def zevix_health():
    return jsonify({"status": "ZEVIX backend running"})

# ----------------------------
# LOGIN (erste Version ohne DB)
# ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def zevix_login():
    data = request.get_json()

    email = data.get("email")
    password = data.get("password")

    # Demo-Login (ersetzen wir später mit Datenbank)
    if email == "admin@zevix.ch" and password == "test123":
        return jsonify({
            "success": True,
            "token": "demo-token-123",
            "user": {
                "email": email
            }
        })

    return jsonify({
        "success": False,
        "message": "Invalid login"
    }), 401
