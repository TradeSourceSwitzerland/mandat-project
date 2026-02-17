import os
import logging
import bcrypt
import jwt
import json
import psycopg
import stripe
import time
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session
from hmac import compare_digest
from psycopg.rows import dict_row
from threading import Lock

# ---------------------------- CONFIG ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")

VALID_PLANS = {"none", "basic", "business", "enterprise"}
DEFAULT_PLAN_BY_PRICE_ID = {
    "prod_TxPBWrcKyJ8EiK": "enterprise",  # Live-Preis-ID für Enterprise
    "prod_TxPABMR85vBl2U": "business",    # Live-Preis-ID für Business
    "prod_TxPAEQ2MB1FblT": "basic",       # Live-Preis-ID für Basic
}

# Cache for Stripe plan syncs (in-memory with TTL)
STRIPE_PLAN_CACHE = {}
STRIPE_PLAN_CACHE_TTL = 300  # 5 minutes
STRIPE_PLAN_CACHE_LOCK = Lock()

# Request deduplication for concurrent calls
ACTIVE_STRIPE_REQUESTS = {}
ACTIVE_STRIPE_REQUESTS_LOCK = Lock()

# Lead limits by plan
LEADS_LIMIT_BY_PLAN = {
    "none": 0,
    "basic": 500,
    "business": 1000,
    "enterprise": 4500,
}

# ---------------------------- HELPERS ----------------------------
def normalize_plan(plan: str | None) -> str:
    value = str(plan or "none").strip().lower()
    return value if value in VALID_PLANS else "none"


def normalize_email_candidate(value: str | None) -> str:
    email = str(value or "").strip().lower()
    if not email or " " in email or email.count("@") != 1:
        return ""
    local, domain = email.split("@", 1)
    if not local or not domain or "." not in domain:
        return ""
    return email

def default_auth_until_ms() -> int:
    return int((datetime.now() + timedelta(days=30)).timestamp() * 1000)


def get_month_key() -> str:
    return datetime.now().strftime("%Y-%m")


def get_leads_limit(plan: str) -> int:
    """
    Returns the monthly lead limit for a given plan.
    
    Args:
        plan: The plan name (basic, business, enterprise, none)
    
    Returns:
        Monthly lead limit (0 for none plan)
    """
    return LEADS_LIMIT_BY_PLAN.get(normalize_plan(plan), 0)


def create_jwt_token(email: str, plan: str, valid_until: int) -> str:
    expiration = datetime.utcnow() + timedelta(days=30)
    payload = {
        "email": email,
        "plan": normalize_plan(plan),
        "valid_until": valid_until,
        "exp": expiration,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def request_payload() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if request.form:
        return request.form.to_dict(flat=True)


def verify_password(password: str, stored_password: str | None) -> bool:
    if not stored_password:
        return False
    return bcrypt.checkpw(password.encode(), stored_password.encode())


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL fehlt")
    return psycopg.connect(f"{DATABASE_URL}?sslmode=require", row_factory=dict_row)


def configured_price_plan_map() -> dict[str, str]:
    mapping = dict(DEFAULT_PLAN_BY_PRICE_ID)
    env_map = {
        os.getenv("STRIPE_PRICE_BASIC"): "basic",
        os.getenv("STRIPE_PRICE_BUSINESS"): "business",
        os.getenv("STRIPE_PRICE_ENTERPRISE"): "enterprise",
        os.getenv("STRIPE_PRODUCT_BASIC"): "basic",
        os.getenv("STRIPE_PRODUCT_BUSINESS"): "business",
        os.getenv("STRIPE_PRODUCT_ENTERPRISE"): "enterprise",
    }
    for price_id, plan in env_map.items():
        if price_id:
            mapping[price_id] = plan
    return mapping


def resolve_email_from_checkout_session(checkout_session: dict) -> str:
    # PRIORITY 1: Check metadata for app email (most reliable for our use case)
    metadata = checkout_session.get("metadata") or {}
    for key in ("app_email", "user_email", "email"):
        email = normalize_email_candidate(metadata.get(key))
        if email:
            return email

    # PRIORITY 2: Check client_reference_id (often used for user identification)
    email = normalize_email_candidate(checkout_session.get("client_reference_id"))
    if email:
        return email

    # PRIORITY 3: Check customer_email (might be different from app email)
    email = normalize_email_candidate(checkout_session.get("customer_email"))
    if email:
        return email

    # PRIORITY 4: Check customer_details
    customer_details = checkout_session.get("customer_details") or {}
    email = normalize_email_candidate(customer_details.get("email"))
    if email:
        return email

    # PRIORITY 5: Last resort - lookup via Stripe Customer
    customer_id = str(checkout_session.get("customer") or "").strip()
    if customer_id:
        try:
            customer = stripe.Customer.retrieve(customer_id)
            email = normalize_email_candidate(customer.get("email"))
            if email:
                return email
        except Exception as exc:
            logging.warning("Stripe customer lookup fehlgeschlagen, customer_id=%s, error=%s", customer_id, exc)

    return ""


def apply_checkout_result_to_user(checkout_session: dict) -> tuple[bool, str]:
    email = resolve_email_from_checkout_session(checkout_session)
    if not email:
        return False, "missing_customer_email"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT plan FROM users WHERE lower(email)=%s", (email,))
            user_row = cur.fetchone()
            if not user_row:
                return False, "user_not_found"

            old_plan = normalize_plan(user_row.get("plan"))
            new_plan = resolve_plan_from_checkout_session(checkout_session)

            # Niemals auf none downgraden, wenn keine belastbare Plan-Info vorliegt
            if new_plan == "none":
                new_plan = old_plan

            if old_plan != new_plan:
                month = get_month_key()
                cur.execute(
                    """
                    UPDATE usage
                    SET plan = %s
                    WHERE user_email = %s AND month = %s
                    """,
                    (new_plan, email, month),
                )
                conn.commit()

    return True, ""


def resolve_plan_from_checkout_session(checkout_session: dict) -> str:
    line_items = (checkout_session.get("line_items") or {}).get("data")
    if not line_items:
        session_id = checkout_session.get("id")
        if not session_id:
            return "none"

        try:
            line_items = stripe.checkout.Session.list_line_items(session_id, limit=10).get("data", [])
        except Exception as exc:
            logging.warning("Stripe line items konnten nicht geladen werden: %s", exc)
            return "none"

    for item in line_items:
        price = item.get("price") or {}
        price_id = price.get("id")
        product_id = price.get("product")
        resolved = normalize_plan(
            DEFAULT_PLAN_BY_PRICE_ID.get(price_id) or DEFAULT_PLAN_BY_PRICE_ID.get(product_id)
        )
        if resolved != "none":
            return resolved

    return "none"


def plan_rank(plan: str) -> int:
    order = {"none": 0, "basic": 1, "business": 2, "enterprise": 3}
    return order.get(normalize_plan(plan), 0)


def subscription_status_rank(status: str | None) -> int:
    order = {
        "active": 4,
        "trialing": 3,
        "past_due": 2,
        "unpaid": 1,
        "incomplete": 0,
        "incomplete_expired": -1,
        "canceled": -2,
    }
    return order.get(str(status or "").strip().lower(), -3)


def resolve_plan_from_subscription(subscription: dict) -> tuple[str, tuple[int, int, int, int]] | None:
    status = str(subscription.get("status") or "").strip().lower()
    status_rank = subscription_status_rank(status)
    if status_rank < 0:
        return None

    plan_by_price_id = configured_price_plan_map()
    items = ((subscription.get("items") or {}).get("data") or [])
    best_plan = "none"

    for item in items:
        price = item.get("price") or {}
        price_id = str(price.get("id") or "").strip()
        product_id = str(price.get("product") or "").strip()
        resolved = normalize_plan(plan_by_price_id.get(price_id) or plan_by_price_id.get(product_id))
        if plan_rank(resolved) > plan_rank(best_plan):
            best_plan = resolved

    period_end = int(subscription.get("current_period_end") or 0)
    created = int(subscription.get("created") or 0)
    score = (status_rank, period_end, created, plan_rank(best_plan))
    return best_plan, score


def sync_user_plan_from_stripe(email: str, current_plan: str, force: bool = False) -> str:
    """
    Syncs user plan from Stripe with caching and optimization.
    
    Args:
        email: User email
        current_plan: Current plan from database
        force: If True, bypass cache and force fresh lookup
    
    Returns:
        Reconciled plan name
    """
    normalized_current_plan = normalize_plan(current_plan)

    if not stripe.api_key:
        logging.warning("STRIPE_SECRET_KEY fehlt: Stripe-Reconciliation deaktiviert, email=%s", email)
        return normalized_current_plan

    # Check cache first (unless forced)
    if not force:
        cached_plan, cache_hit = get_cached_stripe_plan(email)
        if cache_hit:
            # Return cached plan, but only if it's not a downgrade to "none"
            # (we never downgrade from a paid plan to none based on cache alone)
            if cached_plan != "none" or normalized_current_plan == "none":
                return cached_plan

    # Request deduplication: check if another request is already processing this email
    with ACTIVE_STRIPE_REQUESTS_LOCK:
        if email in ACTIVE_STRIPE_REQUESTS:
            # Another request is already syncing this user, return current plan
            # to avoid duplicate API calls
            logging.debug("Deduplication: Stripe sync already in progress for email=%s", email)
            return normalized_current_plan
        # Mark this email as being processed
        ACTIVE_STRIPE_REQUESTS[email] = time.time()

    try:
        start_time = time.time()
        
        # Perform Stripe API calls
        try:
            customers = stripe.Customer.list(email=email, limit=5).get("data", [])
        except Exception as exc:
            logging.warning("Stripe Customer-Suche fehlgeschlagen, email=%s, error=%s", email, exc)
            return normalized_current_plan

        best_candidate: tuple[str, tuple[int, int, int, int]] | None = None

        for customer in customers:
            customer_id = str(customer.get("id") or "").strip()
            if not customer_id:
                continue

            try:
                subscriptions = stripe.Subscription.list(customer=customer_id, status="all", limit=20).get("data", [])
            except Exception as exc:
                logging.warning("Stripe Subscriptions konnten nicht geladen werden, customer_id=%s, error=%s", customer_id, exc)
                continue

            for subscription in subscriptions:
                candidate = resolve_plan_from_subscription(subscription)
                if not candidate:
                    continue
                if best_candidate is None or candidate[1] > best_candidate[1]:
                    best_candidate = candidate

        if best_candidate is None:
            # No active subscription found, cache the current plan
            set_cached_stripe_plan(email, normalized_current_plan)
            elapsed = time.time() - start_time
            logging.info("Stripe sync completed (no subscription), email=%s, elapsed=%.3fs", email, elapsed)
            return normalized_current_plan

        reconciled_plan = normalize_plan(best_candidate[0])

        # Cache the result
        set_cached_stripe_plan(email, reconciled_plan)

        elapsed = time.time() - start_time
        logging.info("Stripe sync completed, email=%s, plan=%s, elapsed=%.3fs", email, reconciled_plan, elapsed)

        # Update database if plan changed
        if reconciled_plan != normalized_current_plan:
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE users
                            SET plan=%s, valid_until=%s
                            WHERE lower(email)=%s
                            """,
                            (reconciled_plan, default_auth_until_ms(), email),
                        )
                    conn.commit()
            except Exception as exc:
                logging.warning("Lokales Plan-Reconciliation fehlgeschlagen, email=%s, error=%s", email, exc)
                return normalized_current_plan

        return reconciled_plan
        
    finally:
        # Remove email from active requests
        with ACTIVE_STRIPE_REQUESTS_LOCK:
            ACTIVE_STRIPE_REQUESTS.pop(email, None)


def find_user_by_email(cur, email: str) -> dict | None:
    cur.execute("SELECT email, password FROM users WHERE lower(email)=%s", (email.lower(),))
    return cur.fetchone()


def resolve_session_id(data: dict) -> str:
    return str(data.get("session_id") or data.get("sessionId") or "").strip()


def get_cached_stripe_plan(email: str) -> tuple[str, bool]:
    """
    Get cached Stripe plan for user.
    Returns: (plan, cache_hit)
    """
    with STRIPE_PLAN_CACHE_LOCK:
        cache_entry = STRIPE_PLAN_CACHE.get(email)
        if cache_entry:
            plan, timestamp = cache_entry
            if time.time() - timestamp < STRIPE_PLAN_CACHE_TTL:
                logging.debug("Cache HIT for Stripe plan sync, email=%s, cached_plan=%s", email, plan)
                return plan, True
            else:
                # Expired entry
                del STRIPE_PLAN_CACHE[email]
                logging.debug("Cache EXPIRED for Stripe plan sync, email=%s", email)
    return "none", False


def set_cached_stripe_plan(email: str, plan: str) -> None:
    """
    Cache Stripe plan for user with current timestamp.
    """
    with STRIPE_PLAN_CACHE_LOCK:
        STRIPE_PLAN_CACHE[email] = (plan, time.time())
        logging.debug("Cache SET for Stripe plan sync, email=%s, plan=%s", email, plan)


def should_sync_stripe_plan(email: str, current_plan: str) -> bool:
    """
    Determines if we should perform a Stripe sync for this user.
    Returns True if:
    - User has no paid plan (plan == "none") - check if they upgraded
    - Cache is expired or missing
    """
    normalized_plan = normalize_plan(current_plan)
    
    # Always check for users with "none" plan (they might have purchased)
    if normalized_plan == "none":
        return True
    
    # For paid plans, check cache
    with STRIPE_PLAN_CACHE_LOCK:
        cache_entry = STRIPE_PLAN_CACHE.get(email)
        if cache_entry:
            _, timestamp = cache_entry
            # If cache is still valid, no need to sync
            if time.time() - timestamp < STRIPE_PLAN_CACHE_TTL:
                return False
    
    # Cache expired or missing for paid plan
    return True


# ---------------------------- Blueprint für ZEVIX ----------------------------
zevix_bp = Blueprint("zevix", __name__)

# ---------------------------- REGISTER ----------------------------
@zevix_bp.route("/zevix/register", methods=["POST"])
def register():
    data = request_payload()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "missing"}), 400

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, password, plan, valid_until)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (email, hashed, "none", default_auth_until_ms()),
                )
                conn.commit()
    except Exception as exc:
        logging.error("Fehler beim Hinzufügen des Benutzers: %s", exc)
        return jsonify({"success": False, "message": "internal_error"}), 500

    return jsonify({"success": True, "message": "registered"})


# ---------------------------- LOGIN ----------------------------
@zevix_bp.route("/zevix/login", methods=["POST"])
def login():
    data = request_payload()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "missing"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            user = find_user_by_email(cur, email)
            if not user:
                return jsonify({"success": False, "message": "not_found"}), 404
            if not verify_password(password, user.get("password")):
                return jsonify({"success": False, "message": "wrong_password"}), 401

    month = get_month_key()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan, valid_until
                FROM users
                WHERE lower(email)=%s
                """,
                (email,),
            )
            user_data = cur.fetchone() or {}

            plan = normalize_plan(user_data.get("plan"))
            valid_until = int(user_data.get("valid_until") or default_auth_until_ms())

            # Only sync with Stripe if necessary (cache miss or no paid plan)
            if should_sync_stripe_plan(email, plan):
                reconciled_plan = sync_user_plan_from_stripe(email, plan)
                if reconciled_plan != plan:
                    plan = reconciled_plan
                    valid_until = default_auth_until_ms()
            else:
                logging.debug("Skipping Stripe sync (cached), email=%s, plan=%s", email, plan)

            cur.execute(
                """
                INSERT INTO usage (user_email, month, used, used_ids)
                VALUES (%s, %s, 0, '[]'::jsonb)
                ON CONFLICT (user_email, month) DO NOTHING
                """,
                (email, month),
            )

            cur.execute(
                """
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
                """,
                (email, month),
            )
            usage = cur.fetchone() or {}
            used = int(usage.get("used") or 0)
            used_ids = usage.get("used_ids") or []
        conn.commit()

    token = create_jwt_token(email, plan, valid_until)

    response = jsonify(
        {
            "success": True,
            "email": email,
            "plan": plan,
            "valid_until": valid_until,
            # Frontend-Kompatibilität (bestehendes Webflow-Script erwartet auth_until)
            "auth_until": valid_until,
            "month": month,
            "used": used,
            "used_ids": used_ids,
            "token": token,
        }
    )

    session["auth_token"] = token
    session["email"] = email
    session["plan"] = plan
    session["used"] = used
    session["used_ids"] = used_ids

    return response


# ---------------------------- LOGOUT ----------------------------
@zevix_bp.route("/zevix/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True, "message": "logged_out"})


# ---------------------------- REFRESH TOKEN ----------------------------
@zevix_bp.route("/zevix/refresh-token", methods=["POST"])
def refresh_token():
    data = request_payload()
    token = data.get("token") or session.get("auth_token") or ""
    
    if not token:
        return jsonify({"success": False, "message": "missing_token"}), 400
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        email = payload.get("email")
        if not email:
            return jsonify({"success": False, "message": "invalid_token"}), 401
    except jwt.ExpiredSignatureError:
        return jsonify({"success": False, "message": "token_expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"success": False, "message": "invalid_token"}), 401
    
    month = get_month_key()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan, valid_until
                FROM users
                WHERE lower(email)=%s
                """,
                (email.lower(),),
            )
            user_data = cur.fetchone()
            
            if user_data is None:
                return jsonify({"success": False, "message": "user_not_found"}), 404
            
            plan = normalize_plan(user_data.get("plan"))
            valid_until = int(user_data.get("valid_until") or default_auth_until_ms())
            
            # Only sync with Stripe if necessary (cache miss or no paid plan)
            if should_sync_stripe_plan(email, plan):
                reconciled_plan = sync_user_plan_from_stripe(email, plan)
                if reconciled_plan != plan:
                    plan = reconciled_plan
                    valid_until = default_auth_until_ms()
            else:
                logging.debug("Skipping Stripe sync (cached), email=%s, plan=%s", email, plan)
            
            cur.execute(
                """
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
                """,
                (email, month),
            )
            usage = cur.fetchone() or {}
            used = int(usage.get("used") or 0)
            used_ids = usage.get("used_ids") or []
    
    new_token = create_jwt_token(email, plan, valid_until)
    
    response = jsonify(
        {
            "success": True,
            "email": email,
            "plan": plan,
            "valid_until": valid_until,
            "auth_until": valid_until,
            "month": month,
            "used": used,
            "used_ids": used_ids,
            "token": new_token,
        }
    )
    
    session["auth_token"] = new_token
    session["email"] = email
    session["plan"] = plan
    session["used"] = used
    session["used_ids"] = used_ids
    
    return response


# ---------------------------- EXPORT LEAD ----------------------------
@zevix_bp.route("/zevix/export-lead", methods=["POST"])
def export_lead():
    """
    Exports a lead and tracks usage against the user's monthly plan limit.
    
    This endpoint:
    - Validates user authentication
    - Checks if user has enough remaining leads for the month
    - Prevents duplicate lead exports (same lead_id twice)
    - Enforces plan-based limits (basic=500, business=1000, enterprise=4500)
    - Updates usage counters and tracks used lead IDs
    """
    # Check if user is logged in (via Bearer token or session)
    # Try Bearer token from Authorization header first
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""
    user_email = None
    
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_email = payload.get("email")
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            # Token invalid or expired, will try session fallback
            pass
    
    # Fall back to session-based authentication
    if not user_email:
        user_email = session.get("email")
    
    if not user_email:
        return jsonify({"success": False, "error": "not_authenticated"}), 401
    
    data = request_payload()
    lead_data = data.get("lead_data")
    if not lead_data:
        return jsonify({"success": False, "error": "missing_lead_data"}), 400
    
    # Extract lead_id from lead_data (could be a dict with id or just a string ID)
    if isinstance(lead_data, dict):
        lead_id = lead_data.get("id") or lead_data.get("lead_id")
    else:
        lead_id = str(lead_data)
    
    if not lead_id:
        return jsonify({"success": False, "error": "missing_lead_id"}), 400
    
    month = get_month_key()
    
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Get user's current plan
            cur.execute(
                """
                SELECT plan
                FROM users
                WHERE lower(email)=%s
                """,
                (user_email.lower(),),
            )
            user_data = cur.fetchone()
            
            if not user_data:
                return jsonify({"success": False, "error": "user_not_found"}), 404
            
            plan = normalize_plan(user_data.get("plan"))
            limit = get_leads_limit(plan)
            
            # If user has no plan or limit is 0, deny export
            if limit == 0:
                return jsonify({
                    "success": False,
                    "error": "no_plan",
                    "message": "You need an active plan to export leads"
                }), 403
            
            # Ensure usage record exists for this month
            cur.execute(
                """
                INSERT INTO usage (user_email, month, used, used_ids, plan)
                VALUES (%s, %s, 0, '[]'::jsonb, %s)
                ON CONFLICT (user_email, month) DO NOTHING
                """,
                (user_email, month, plan),
            )
            
            # Get current usage
            cur.execute(
                """
                SELECT used, used_ids
                FROM usage
                WHERE user_email=%s AND month=%s
                """,
                (user_email, month),
            )
            usage = cur.fetchone() or {}
            used = int(usage.get("used", 0))
            used_ids_raw = usage.get("used_ids")
            
            # Ensure used_ids is a list (handle both JSONB and string cases)
            if isinstance(used_ids_raw, str):
                used_ids = json.loads(used_ids_raw) if used_ids_raw else []
            elif isinstance(used_ids_raw, list):
                used_ids = used_ids_raw
            else:
                used_ids = []
            
            # Check for duplicate lead_id
            if lead_id in used_ids:
                return jsonify({
                    "success": False,
                    "error": "lead_already_used",
                    "message": "This lead has already been exported",
                    "used": used,
                    "remaining": limit - used,
                    "limit": limit
                }), 409
            
            # Check if user has reached their monthly limit
            if used >= limit:
                return jsonify({
                    "success": False,
                    "error": "monthly_limit_exceeded",
                    "message": f"You have 0 leads remaining ({used}/{limit})",
                    "used": used,
                    "remaining": 0,
                    "limit": limit
                }), 403
            
            # Increment usage and add lead_id to used_ids
            new_used = used + 1
            new_used_ids = used_ids + [lead_id]
            
            cur.execute(
                """
                UPDATE usage
                SET used = %s, used_ids = %s::jsonb
                WHERE user_email = %s AND month = %s
                """,
                (new_used, json.dumps(new_used_ids), user_email, month),
            )
            conn.commit()
            
            # Update session with new values
            session["used"] = new_used
            session["used_ids"] = new_used_ids
            
            remaining = limit - new_used
            
            return jsonify({
                "success": True,
                "used": new_used,
                "remaining": remaining,
                "limit": limit,
                "lead_id": lead_id,
                "month": month,
                "message": f"Lead exported successfully. {remaining} leads remaining"
            })


# ---------------------------- CREATE CHECKOUT SESSION ----------------------------
@zevix_bp.route("/zevix/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """
    Creates a Stripe checkout session for the logged-in user.
    Automatically uses the user's app email from session for proper synchronization.
    """
    # Check if user is logged in
    user_email = session.get("email")
    if not user_email:
        return jsonify({"success": False, "message": "not_authenticated"}), 401
    
    data = request_payload()
    price_id = data.get("price_id")
    success_url = data.get("success_url")
    cancel_url = data.get("cancel_url")
    
    if not price_id:
        return jsonify({"success": False, "message": "missing_price_id"}), 400
    
    if not success_url:
        return jsonify({"success": False, "message": "missing_success_url"}), 400
    
    if not cancel_url:
        cancel_url = success_url  # Use success_url as fallback
    
    try:
        # Create Stripe checkout session with user's app email
        checkout_session = stripe.checkout.Session.create(
            customer_email=user_email,  # Use app email for Stripe checkout
            client_reference_id=user_email,  # Additional backup for email
            metadata={
                "app_email": user_email,  # Highest priority in resolve function
                "user_email": user_email,  # Backup metadata field
            },
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            mode="subscription",
            success_url=success_url,
            cancel_url=cancel_url,
        )
        
        logging.info(
            "Checkout session created successfully, session_id=%s, user_email=%s, price_id=%s",
            checkout_session.get("id"),
            user_email,
            price_id,
        )
        
        return jsonify({
            "success": True,
            "session_id": checkout_session.get("id"),
            "url": checkout_session.get("url"),
        })
        
    except Exception as exc:
        logging.error("Failed to create checkout session, user_email=%s, error=%s", user_email, exc)
        return jsonify({"success": False, "message": "checkout_creation_failed"}), 500


# ---------------------------- VERIFY SESSION ----------------------------
@zevix_bp.route("/zevix/verify-session", methods=["POST"])
def verify_session():
    data = request_payload()
    session_id = resolve_session_id(data)
    logging.debug("verify-session aufgerufen, session_id=%s", session_id)
    if not session_id:
        return jsonify(success=False, message="missing_session_id"), 400

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id, expand=["line_items.data.price"])
    except Exception as exc:
        logging.warning("Stripe Session konnte nicht geladen werden, session_id=%s, error=%s", session_id, exc)
        return jsonify(success=False, message="invalid_session"), 400

    payment_status = str(checkout_session.get("payment_status") or "").lower()
    session_status = str(checkout_session.get("status") or "").lower()
    logging.debug(
        "Stripe Session geladen, session_id=%s, payment_status=%s, session_status=%s",
        session_id,
        payment_status,
        session_status,
    )

    free_or_trial_statuses = {"pending", "unpaid", "no_payment_required"}
    if payment_status in free_or_trial_statuses or session_status == "open":
        logging.info(
            "Session in Testphase/kostenfrei/offen, Dashboard-Redirect erlaubt: session_id=%s, payment_status=%s, session_status=%s",
            session_id,
            payment_status,
            session_status,
        )
        updated, message = apply_checkout_result_to_user(checkout_session)
        if not updated:
            logging.warning(
                "Trial-/Free-Session konnte nicht vollständig synchronisiert werden, session_id=%s, message=%s",
                session_id,
                message,
            )
        return jsonify(success=True, message="in_trial_or_free")

    if payment_status != "paid" and session_status != "complete":
        logging.info(
            "Zahlung nicht abgeschlossen, session_id=%s, payment_status=%s, session_status=%s",
            session_id,
            payment_status,
            session_status,
        )
        return jsonify(success=False, message="payment_not_completed"), 409

    updated, message = apply_checkout_result_to_user(checkout_session)
    if not updated:
        logging.warning("User-Update fehlgeschlagen, session_id=%s, message=%s", session_id, message)
        if message in {"missing_customer_email", "user_not_found"}:
            logging.info(
                "Checkout abgeschlossen, aber Sync noch ausstehend; Dashboard-Redirect erlaubt: session_id=%s",
                session_id,
            )
            return jsonify(success=True, message="sync_pending")

        status_code = 404 if message == "user_not_found" else 400
        return jsonify(success=False, message=message), status_code

    logging.info("verify-session erfolgreich, session_id=%s", session_id)

    return jsonify(success=True)


# ---------------------------- CACHE STATS (ADMIN/MONITORING) ----------------------------
@zevix_bp.route("/zevix/cache-stats", methods=["GET"])
def cache_stats():
    """
    Returns cache statistics for monitoring performance improvements.
    Useful for debugging and verifying cache is working.
    """
    with STRIPE_PLAN_CACHE_LOCK:
        cache_size = len(STRIPE_PLAN_CACHE)
        cached_emails = list(STRIPE_PLAN_CACHE.keys())
        
        # Count how many entries are still valid
        valid_count = 0
        expired_count = 0
        for email, (plan, timestamp) in list(STRIPE_PLAN_CACHE.items()):
            if time.time() - timestamp < STRIPE_PLAN_CACHE_TTL:
                valid_count += 1
            else:
                expired_count += 1
    
    with ACTIVE_STRIPE_REQUESTS_LOCK:
        active_requests = len(ACTIVE_STRIPE_REQUESTS)
    
    return jsonify({
        "success": True,
        "cache": {
            "total_entries": cache_size,
            "valid_entries": valid_count,
            "expired_entries": expired_count,
            "ttl_seconds": STRIPE_PLAN_CACHE_TTL,
        },
        "active_requests": active_requests,
        "performance": {
            "cache_enabled": True,
            "deduplication_enabled": True,
            "expected_api_reduction": "60-80%",
        }
    })
