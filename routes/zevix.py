from flask import Blueprint, jsonify, request

# Blueprint muss GENAU so heißen wie wir ihn importieren!
zevix_bp = Blueprint("zevix", __name__)

# Test-Route
@zevix_bp.route("/zevix/health", methods=["GET"])
def zevix_health():
    return jsonify({"status": "ZEVIX backend running"})
