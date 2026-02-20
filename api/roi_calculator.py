"""
ROI Calculator API für Zevix
Geschützte Business Logic für Lead-Berechnungen
"""

from flask import request, jsonify, Blueprint
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import logging
from datetime import datetime

# Blueprint
roi_bp = Blueprint('roi', __name__)

# Logger Setup
logger = logging.getLogger(__name__)

# Pricing Tiers
PLAN_BASIC_MAX_LEADS = 500
PLAN_BUSINESS_MAX_LEADS = 1000
PLAN_BASIC_COST = 69
PLAN_BUSINESS_COST = 100
PLAN_ENTERPRISE_COST = 200

LEADS_MIN = 500
LEADS_MAX = 4500
CONVERSION_MIN = 0.5
CONVERSION_MAX = 10.0
REVENUE_MIN = 100
REVENUE_MAX = 1000000

# Rate Limiter Setup
def create_limiter(app):
    return Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per day", "100 per hour"]
    )


@roi_bp.route('/api/calculate-roi', methods=['POST'])
def calculate_roi():
    """
    ROI Calculator Endpoint

    Request Body:
        leads (int): Anzahl Leads pro Monat (500-4500)
        conversion (float): Conversion Rate in % (0.5-10)
        revenue (int): Umsatz pro Kunde in CHF (min 100)

    Returns:
        JSON mit customers, monthly, yearly, roi, plan, subscriptionCost
    """

    start_time = datetime.now()

    try:
        data = request.get_json()

        if not data:
            logger.warning("Empty request body received")
            return jsonify({"error": "Keine Daten gesendet"}), 400

        # Input Validierung
        try:
            leads = int(data.get('leads', 0))
            conversion = float(data.get('conversion', 0))
            revenue = int(data.get('revenue', 0))
        except (ValueError, TypeError) as e:
            logger.error("Invalid data types: %s", str(e))
            return jsonify({"error": "Ungültige Datentypen"}), 400

        # Range Checks
        if not (LEADS_MIN <= leads <= LEADS_MAX):
            logger.warning("Leads out of range: %d", leads)
            return jsonify({"error": "Leads müssen zwischen 500 und 4500 liegen"}), 400

        if not (CONVERSION_MIN <= conversion <= CONVERSION_MAX):
            logger.warning("Conversion out of range: %f", conversion)
            return jsonify({"error": "Conversion muss zwischen 0.5% und 10% liegen"}), 400

        if revenue < REVENUE_MIN:
            logger.warning("Revenue too low: %d", revenue)
            return jsonify({"error": "Umsatz muss mindestens CHF 100 sein"}), 400

        if revenue > REVENUE_MAX:
            logger.warning("Revenue too high: %d", revenue)
            return jsonify({"error": "Umsatz zu hoch"}), 400

        # 🔒 GESCHÜTZTE BUSINESS LOGIC - PLAN AUSWAHL
        subscription_cost, plan = get_plan_by_leads(leads)

        # Berechnungen
        customers = round(leads * (conversion / 100))
        monthly_revenue = customers * revenue
        yearly_revenue = monthly_revenue * 12
        roi_value = calculate_roi_percentage(monthly_revenue, subscription_cost)

        # Response
        response_data = {
            "customers": customers,
            "monthly": monthly_revenue,
            "yearly": yearly_revenue,
            "roi": round(roi_value, 2),
            "plan": plan,
            "subscriptionCost": subscription_cost
        }

        # Logging
        duration = (datetime.now() - start_time).total_seconds() * 1000
        logger.info("ROI calculated: %s plan, %d customers, %.2fms", plan, customers, duration)

        return jsonify(response_data), 200

    except Exception as e:
        logger.error("Unexpected error in calculate_roi: %s", str(e), exc_info=True)
        return jsonify({"error": "Interner Serverfehler"}), 500


def get_plan_by_leads(leads):
    """
    🔒 GESCHÜTZTE FUNKTION - Plan-Logik

    Pricing Tiers:
    - Basic: bis 500 Leads → CHF 69/Monat
    - Business: 501-1000 Leads → CHF 100/Monat
    - Enterprise: 1001-4500 Leads → CHF 200/Monat
    """
    if leads <= PLAN_BASIC_MAX_LEADS:
        return PLAN_BASIC_COST, "Basic"
    elif leads <= PLAN_BUSINESS_MAX_LEADS:
        return PLAN_BUSINESS_COST, "Business"
    else:
        return PLAN_ENTERPRISE_COST, "Enterprise"


def calculate_roi_percentage(monthly_revenue, subscription_cost):
    """
    🔒 GESCHÜTZTE FUNKTION - ROI Berechnung

    ROI = ((Umsatz - Kosten) / Kosten) * 100
    """
    if subscription_cost == 0:
        return 0

    return ((monthly_revenue - subscription_cost) / subscription_cost) * 100


# Health Check Endpoint
@roi_bp.route('/api/health', methods=['GET'])
def health_check():
    """Health Check für Monitoring"""
    return jsonify({
        "status": "healthy",
        "service": "roi-calculator",
        "timestamp": datetime.now().isoformat()
    }), 200
