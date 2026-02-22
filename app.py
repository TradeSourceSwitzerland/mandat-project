# ----------------------------
# Health‑Check für Wake‑Up Pings
# ----------------------------
@app.route("/")
@app.route("/healthz")
def healthz():
    return jsonify({"status": "healthy", "service": "Mandat Backend"}), 200