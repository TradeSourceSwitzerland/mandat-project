"""
ROI Calculator API für Zevix
Geschützte Business Logic
"""

from flask import Blueprint, request, jsonify
import logging
from datetime import datetime

roi_bp = Blueprint('roi', __name__)
logger = logging.getLogger(__name__)


@roi_bp.route('/api/calculate-roi', methods=['POST'])
def calculate_roi():
    """
    ROI Calculator Endpoint

    Request Body:
        leads (int): 500-4500
        conversion (float): 0.5-10
        revenue (int): min 100

    Returns:
        JSON: {customers, monthly, yearly, roi, plan, subscriptionCost}
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({"error": "Keine Daten gesendet"}), 400

        # Input Validierung
        try:
            leads = int(data.get('leads', 0))
            conversion = float(data.get('conversion', 0))
            revenue = int(data.get('revenue', 0))
        except (ValueError, TypeError):
            return jsonify({"error": "Ungültige Datentypen"}), 400

        # Range Checks
        if not (500 <= leads <= 4500):
            return jsonify({"error": "Leads außerhalb des Bereichs"}), 400

        if not (0.5 <= conversion <= 10):
            return jsonify({"error": "Conversion außerhalb des Bereichs"}), 400

        if revenue < 100 or revenue > 1000000:
            return jsonify({"error": "Ungültiger Umsatz"}), 400

        # 🔒 GESCHÜTZTE BUSINESS LOGIC
        # Plan-Auswahl basierend auf Leads
        if leads <= 500:
            subscription_cost = 69
            plan = "Basic"
        elif leads <= 1000:
            subscription_cost = 100
            plan = "Business"
        else:
            subscription_cost = 200
            plan = "Enterprise"

        # ROI Berechnungen
        customers = round(leads * (conversion / 100))
        monthly_revenue = customers * revenue
        yearly_revenue = monthly_revenue * 12
        roi_value = ((monthly_revenue - subscription_cost) / subscription_cost) * 100

        # Response
        response_data = {
            "customers": customers,
            "monthly": monthly_revenue,
            "yearly": yearly_revenue,
            "roi": round(roi_value, 2),
            "plan": plan,
            "subscriptionCost": subscription_cost
        }

        logger.info("ROI calculated: %s plan, %d customers", plan, customers)
        return jsonify(response_data), 200

    except Exception as e:
        logger.error("Error in calculate_roi: %s", str(e))
        return jsonify({"error": "Interner Serverfehler"}), 500


@roi_bp.route('/api/health', methods=['GET'])
def health_check():
    """Health Check"""
    return jsonify({
        "status": "healthy",
        "service": "roi-calculator",
        "timestamp": datetime.now().isoformat()
    }), 200
