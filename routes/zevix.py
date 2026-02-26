import os
import logging
import bcrypt
import jwt
import json
import psycopg
import stripe
import time
import requests as http_requests
from datetime import datetime, timedelta, date
from flask import Blueprint, jsonify, request, session
from hmac import compare_digest
from psycopg.rows import dict_row
from threading import Lock
import openai

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

# ---------------------------- SHAB CONFIG ----------------------------
ALLE_KANTONE = [
    "AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GL", "GR",
    "JU", "LU", "NE", "NW", "OW", "SG", "SH", "SO", "SZ", "TG",
    "TI", "UR", "VD", "VS", "ZG", "ZH",
]

RECHTSFORMEN = {
    "0101": "Einzelunternehmen",
    "0103": "Kollektivgesellschaft",
    "0104": "Kommanditgesellschaft",
    "0106": "Aktiengesellschaft",
    "0107": "GmbH",
    "0108": "Genossenschaft",
    "0109": "Verein",
    "0110": "Stiftung",
    "0113": "Zweigniederlassung",
}

SHAB_API_URL = "https://www.shab.ch/api/v1/publications"

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

def parse_date_to_iso(date_str: str) -> str:
    """Convert various date formats to ISO 8601 (YYYY-MM-DD).

    Supports:
    - MM/DD/YYYY (American: 02/25/2026)
    - DD/MM/YYYY (European: 25/02/2026)
    - YYYY-MM-DD (ISO: 2026-02-25)
    - DD.MM.YYYY (Swiss/German: 25.02.2026)
    """
    if not date_str:
        return date_str

    formats = [
        "%Y-%m-%d",  # ISO - already correct
        "%m/%d/%Y",  # American
        "%d/%m/%Y",  # European
        "%d.%m.%Y",  # Swiss/German
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    logging.warning("Could not parse date format: %s, using as-is", date_str)
    return date_str


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
                cur.execute(
                    """
                    UPDATE users
                    SET plan = %s, valid_until = %s
                    WHERE lower(email) = %s
                    """,
                    (new_plan, default_auth_until_ms(), email),
                )
                conn.commit()
                
                # Invalidate cache for immediate synchronization
                set_cached_stripe_plan(email, new_plan)

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
            # Only use cache when BOTH plans are not "none" (paid → paid transition)
            # When plan is "none", always check Stripe fresh (user might have just purchased)
            if cached_plan != "none" and normalized_current_plan != "none":
                return cached_plan
            # When plan is "none", skip cache and let Stripe check run

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


# ---------------------------- SHAB HELPERS ----------------------------
def ai_branche(zweck: str) -> str:
    """Classify a company's industry sector using OpenAI GPT based on its purpose text."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not zweck:
        return "Sonstige"
    try:
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Du bist Handelsregister-Analyst. Ordne Firmen anhand ihres Zwecks EINER Branche zu. "
                        "Antworte NUR mit einem Branchentitel (1-3 Wörter).\n\n"
                        "ERLAUBTE BRANCHEN:\n"
                        "Autohandel, IT / Software, Gastronomie, Transport / Logistik, "
                        "Baugewerbe, Immobilien, Handel, Industrie, Dienstleistungen, "
                        "Gesundheitswesen, Sonstige"
                    ),
                },
                {"role": "user", "content": zweck[:500]},
            ],
            max_tokens=20,
            temperature=0,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        logging.warning("GPT Branchenklassifizierung fehlgeschlagen: %s", exc)
        return "Sonstige"


def fetch_shab_neueintragungen(datum_von: str, datum_bis: str) -> list:
    """Fetch HR01 (new company registrations) from the official SHAB.ch API."""
    # Convert dates to ISO format
    datum_von = parse_date_to_iso(datum_von)
    datum_bis = parse_date_to_iso(datum_bis)

    params = {
        "allowRubricSelection": "true",
        "cantons": ",".join(ALLE_KANTONE),
        "includeContent": "true",
        "pageRequest.page": "0",
        "pageRequest.size": "2000",
        "publicationStates": "PUBLISHED",
        "publicationDate.start": datum_von,
        "publicationDate.end": datum_bis,
        "subRubrics": "HR01",
    }

    headers = {
        "Accept": "application/json",
        "Accept-Language": "de",
    }

    try:
        resp = http_requests.get(SHAB_API_URL, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("content", [])
        logging.info("SHAB API: %d HR01 entries fetched for %s to %s", len(results), datum_von, datum_bis)
        return results

    except Exception as exc:
        logging.warning("SHAB API error: %s", exc)
        return []


def ensure_leads_table(cur) -> None:
    """Create the leads table if it doesn't exist yet."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id SERIAL PRIMARY KEY,
            uid VARCHAR(20) UNIQUE,
            firma VARCHAR(500),
            rechtsform VARCHAR(100),
            strasse VARCHAR(200),
            hausnummer VARCHAR(20),
            plz VARCHAR(10),
            ort VARCHAR(100),
            sitz VARCHAR(100),
            kanton VARCHAR(5),
            zweck TEXT,
            branche_ai VARCHAR(100),
            publikation_datum DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_datum ON leads(publikation_datum)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_kanton ON leads(kanton)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_branche ON leads(branche_ai)")


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
    email = ""
    try:
        logging.info("Login attempt received from %s", request.remote_addr)

        data = request_payload()
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""

        logging.debug("Login attempt for email: %s", email)

        if not email or not password:
            logging.warning("Login failed: missing credentials for %s", email)
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

        logging.info("Login successful for %s, plan: %s", email, plan)
        return response

    except Exception as exc:
        logging.error("Login error for %s: %s", email, exc, exc_info=True)
        return jsonify({"success": False, "message": "internal_error"}), 500


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
    token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()  # Remove "Bearer " prefix (case-insensitive)
    
    user_email = None
    
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_email = payload.get("email")
        except jwt.ExpiredSignatureError:
            logging.warning("JWT token expired for export-lead request")
        except jwt.InvalidTokenError:
            logging.warning("Invalid JWT token for export-lead request")
    
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
                INSERT INTO usage (user_email, month, used, used_ids)
                VALUES (%s, %s, 0, '[]'::jsonb)
                ON CONFLICT (user_email, month) DO NOTHING
                """,
                (user_email, month),
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


# ---------------------------- EXPORT LEADS BATCH ----------------------------
@zevix_bp.route("/zevix/export-leads-batch", methods=["POST"])
def export_leads_batch():
    """
    Batch export multiple leads and tracks usage against the user's monthly plan limit.
    
    This endpoint:
    - Validates user authentication via Bearer token
    - Accepts a list of lead IDs
    - Filters out already exported IDs (duplicates)
    - Counts only new leads against monthly limit
    - Returns used, remaining, limit, new_ids, and duplicate_ids
    """
    # Check if user is logged in (via Bearer token or session)
    # Try Bearer token from Authorization header first
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()  # Remove "Bearer " prefix (case-insensitive)
    
    user_email = None
    
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_email = payload.get("email")
        except jwt.ExpiredSignatureError:
            logging.warning("JWT token expired for export-leads-batch request")
        except jwt.InvalidTokenError:
            logging.warning("Invalid JWT token for export-leads-batch request")
    
    # Fall back to session-based authentication
    if not user_email:
        user_email = session.get("email")
    
    if not user_email:
        return jsonify({"success": False, "error": "not_authenticated"}), 401
    
    data = request_payload()
    lead_ids = data.get("lead_ids")
    
    if not lead_ids or not isinstance(lead_ids, list):
        return jsonify({"success": False, "error": "missing_lead_ids"}), 400
    
    # Filter out empty lead IDs
    lead_ids = [str(lid).strip() for lid in lead_ids if lid]
    
    if not lead_ids:
        return jsonify({"success": False, "error": "no_valid_lead_ids"}), 400
    
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
                INSERT INTO usage (user_email, month, used, used_ids)
                VALUES (%s, %s, 0, '[]'::jsonb)
                ON CONFLICT (user_email, month) DO NOTHING
                """,
                (user_email, month),
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
            
            # Convert to set for O(n) performance instead of O(n²)
            used_ids_set = set(used_ids)
            
            # Filter out duplicates - only keep lead IDs that haven't been used yet
            new_ids = [lid for lid in lead_ids if lid not in used_ids_set]
            duplicate_ids = [lid for lid in lead_ids if lid in used_ids_set]
            
            # Calculate how many new leads we can actually export
            remaining_before = limit - used
            can_export = min(len(new_ids), remaining_before)
            
            if can_export == 0:
                # Only block if monthly limit is actually exhausted
                if remaining_before == 0:
                    return jsonify({
                        "success": False,
                        "error": "monthly_limit_exceeded",
                        "message": f"You have 0 leads remaining ({used}/{limit})",
                        "used": used,
                        "remaining": 0,
                        "limit": limit,
                        "new_ids": [],
                        "duplicate_ids": duplicate_ids
                    }), 403
                else:
                    # All duplicates - allow export, but don't consume any leads
                    lead_word = "lead" if len(duplicate_ids) == 1 else "leads"
                    return jsonify({
                        "success": True,
                        "used": used,
                        "remaining": remaining_before,
                        "limit": limit,
                        "new_ids": [],
                        "duplicate_ids": duplicate_ids,
                        "not_exported": [],
                        "month": month,
                        "message": f"All {len(duplicate_ids)} {lead_word} already exported (no consumption). {remaining_before} leads remaining"
                    })
            
            # Only export the leads we can afford
            ids_to_export = new_ids[:can_export]
            ids_not_exported = new_ids[can_export:]
            
            # Increment usage and add lead_ids to used_ids
            new_used = used + len(ids_to_export)
            new_used_ids = used_ids + ids_to_export
            
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
                "new_ids": ids_to_export,
                "duplicate_ids": duplicate_ids,
                "not_exported": ids_not_exported,
                "month": month,
                "message": f"Successfully exported {len(ids_to_export)} lead(s). {remaining} leads remaining"
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


# ---------------------------- SYNC SHAB ----------------------------
@zevix_bp.route("/zevix/sync-shab", methods=["POST"])
def sync_shab():
    """
    Fetches new company registrations (HR01) from the SHAB API,
    classifies their industry via GPT and upserts them into the leads table.
    Requires a valid JWT token (admin/authenticated users only).
    """
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    user_email = None
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_email = payload.get("email")
        except jwt.ExpiredSignatureError:
            return jsonify({"success": False, "error": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"success": False, "error": "invalid_token"}), 401

    if not user_email:
        user_email = session.get("email")

    if not user_email:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    data = request_payload() or {}
    today = date.today().isoformat()
    datum_von = parse_date_to_iso(data.get("datum_von") or today)
    datum_bis = parse_date_to_iso(data.get("datum_bis") or today)

    publications = fetch_shab_neueintragungen(datum_von, datum_bis)

    inserted = 0
    updated = 0
    errors = 0

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure_leads_table(cur)
                conn.commit()

                for pub in publications:
                    try:
                        meta = pub.get("meta", {})
                        content = pub.get("content", {})
                        commons = content.get("commonsNew", {}) or content.get("commonsActual", {})
                        company = commons.get("company", {})
                        address = company.get("address", {})

                        uid = company.get("uid") or ""
                        if not uid:
                            continue

                        firma = company.get("name") or ""

                        # Legal form code to name
                        rechtsform_code = company.get("legalForm") or ""
                        rechtsform = RECHTSFORMEN.get(str(rechtsform_code), str(rechtsform_code))

                        # Address fields
                        strasse = address.get("street") or ""
                        hausnummer = address.get("houseNumber") or ""
                        plz = str(address.get("swissZipCode") or "")
                        ort = address.get("town") or ""

                        sitz = company.get("seat") or ort

                        # Canton from meta
                        cantons = meta.get("cantons") or []
                        kanton = cantons[0] if cantons else ""

                        zweck = commons.get("purpose") or ""

                        # Publication date
                        pub_date_raw = meta.get("publicationDate") or ""
                        try:
                            pub_date = pub_date_raw[:10] if pub_date_raw else None
                        except Exception:
                            pub_date = None

                        branche = ai_branche(zweck)

                        cur.execute(
                            """
                            INSERT INTO leads
                                (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                                 sitz, kanton, zweck, branche_ai, publikation_datum)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (uid) DO UPDATE SET
                                firma = EXCLUDED.firma,
                                rechtsform = EXCLUDED.rechtsform,
                                strasse = EXCLUDED.strasse,
                                hausnummer = EXCLUDED.hausnummer,
                                plz = EXCLUDED.plz,
                                ort = EXCLUDED.ort,
                                sitz = EXCLUDED.sitz,
                                kanton = EXCLUDED.kanton,
                                zweck = EXCLUDED.zweck,
                                branche_ai = EXCLUDED.branche_ai,
                                publikation_datum = EXCLUDED.publikation_datum
                            RETURNING (xmax = 0) AS inserted
                            """,
                            (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                             sitz, kanton, zweck, branche, pub_date)
                        )

                        row = cur.fetchone()
                        if row and row.get("inserted"):
                            inserted += 1
                        else:
                            updated += 1
                    except Exception as exc:
                        logging.warning("Fehler beim Verarbeiten eines SHAB-Eintrags: %s", exc)
                        errors += 1
                        continue

                conn.commit()
    except Exception as exc:
        logging.error("SHAB-Sync fehlgeschlagen: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({
        "success": True,
        "datum_von": datum_von,
        "datum_bis": datum_bis,
        "total": len(publications),
        "inserted": inserted,
        "updated": updated,
        "errors": errors,
    })


# ---------------------------- CRON SYNC ----------------------------
@zevix_bp.route("/zevix/cron-sync", methods=["POST"])
def cron_sync():
    """
    Daily cron job to sync SHAB data automatically.
    Protected by a secret key (not user auth).
    """
    cron_secret = request.headers.get("X-Cron-Secret") or (request.get_json(silent=True) or {}).get("cron_secret")
    expected_secret = os.getenv("CRON_SECRET")

    if not expected_secret or cron_secret != expected_secret:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    logging.info("CRON: Starting daily SHAB sync for %s", yesterday)

    try:
        publications = fetch_shab_neueintragungen(yesterday, yesterday)
    except Exception as exc:
        logging.error("CRON: SHAB API error: %s", exc)
        return jsonify({"success": False, "error": f"SHAB API error: {exc}"}), 500

    if not publications:
        logging.info("CRON: No new entries found for %s", yesterday)
        return jsonify({
            "success": True,
            "message": "No new entries",
            "date": yesterday,
            "total": 0
        })

    inserted = 0
    updated = 0
    errors = 0

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure_leads_table(cur)
                conn.commit()

                for pub in publications:
                    try:
                        meta = pub.get("meta", {})
                        content = pub.get("content", {})
                        commons = content.get("commonsNew", {}) or content.get("commonsActual", {})
                        company = commons.get("company", {})
                        address = company.get("address", {})

                        uid = company.get("uid") or ""
                        if not uid:
                            continue

                        firma = company.get("name") or ""
                        rechtsform_code = company.get("legalForm") or ""
                        rechtsform = RECHTSFORMEN.get(str(rechtsform_code), str(rechtsform_code))

                        strasse = address.get("street") or ""
                        hausnummer = address.get("houseNumber") or ""
                        plz = str(address.get("swissZipCode") or "")
                        ort = address.get("town") or ""
                        sitz = company.get("seat") or ort

                        cantons = meta.get("cantons") or []
                        kanton = cantons[0] if cantons else ""

                        zweck = commons.get("purpose") or ""

                        pub_date_raw = meta.get("publicationDate") or ""
                        pub_date = pub_date_raw[:10] if pub_date_raw else None

                        # GPT Klassifizierung - mit Fehlerbehandlung
                        try:
                            branche = ai_branche(zweck) if zweck else ""
                        except Exception as gpt_err:
                            logging.warning("CRON: GPT error for %s: %s", uid, gpt_err)
                            branche = ""

                        cur.execute(
                            """
                            INSERT INTO leads
                                (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                                 sitz, kanton, zweck, branche_ai, publikation_datum)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (uid) DO UPDATE SET
                                firma = EXCLUDED.firma,
                                rechtsform = EXCLUDED.rechtsform,
                                strasse = EXCLUDED.strasse,
                                hausnummer = EXCLUDED.hausnummer,
                                plz = EXCLUDED.plz,
                                ort = EXCLUDED.ort,
                                sitz = EXCLUDED.sitz,
                                kanton = EXCLUDED.kanton,
                                zweck = EXCLUDED.zweck,
                                branche_ai = EXCLUDED.branche_ai,
                                publikation_datum = EXCLUDED.publikation_datum
                            RETURNING (xmax = 0) AS inserted
                            """,
                            (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                             sitz, kanton, zweck, branche, pub_date)
                        )

                        row = cur.fetchone()
                        if row and row.get("inserted"):
                            inserted += 1
                        else:
                            updated += 1

                        # Commit nach jedem Lead (verhindert lange Transaktionen)
                        conn.commit()

                    except Exception as exc:
                        logging.warning("CRON: Error processing %s: %s", uid if uid else "unknown", exc)
                        errors += 1
                        continue

                conn.commit()

    except Exception as exc:
        logging.error("CRON: Database error: %s", exc)
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500

    logging.info("CRON: Completed - inserted=%d, updated=%d, errors=%d", inserted, updated, errors)

    return jsonify({
        "success": True,
        "date": yesterday,
        "total": len(publications),
        "inserted": inserted,
        "updated": updated,
        "errors": errors
    })


# ---------------------------- ADMIN SYNC RANGE ----------------------------
@zevix_bp.route("/zevix/admin/sync-range", methods=["POST"])
def admin_sync_range():
    """Admin endpoint to sync a specific date range (for backfilling data)."""
    cron_secret = request.headers.get("X-Cron-Secret") or (request.get_json(silent=True) or {}).get("cron_secret")
    expected_secret = os.getenv("CRON_SECRET")

    if not expected_secret or cron_secret != expected_secret:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    datum_von = data.get("datum_von")
    datum_bis = data.get("datum_bis")

    if not datum_von or not datum_bis:
        return jsonify({"success": False, "error": "datum_von and datum_bis required"}), 400

    datum_von = parse_date_to_iso(datum_von)
    datum_bis = parse_date_to_iso(datum_bis)

    logging.info("ADMIN SYNC: Starting SHAB sync for %s to %s", datum_von, datum_bis)

    publications = fetch_shab_neueintragungen(datum_von, datum_bis)

    if not publications:
        logging.info("ADMIN SYNC: No entries found for %s to %s", datum_von, datum_bis)
        return jsonify({
            "success": True,
            "message": "No entries found",
            "datum_von": datum_von,
            "datum_bis": datum_bis,
            "total": 0
        })

    inserted = 0
    updated = 0
    errors = 0

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure_leads_table(cur)
                conn.commit()

                for pub in publications:
                    try:
                        meta = pub.get("meta", {})
                        content = pub.get("content", {})
                        commons = content.get("commonsNew", {}) or content.get("commonsActual", {})
                        company = commons.get("company", {})
                        address = company.get("address", {})

                        uid = company.get("uid") or ""
                        if not uid:
                            continue

                        firma = company.get("name") or ""
                        rechtsform_code = company.get("legalForm") or ""
                        rechtsform = RECHTSFORMEN.get(str(rechtsform_code), str(rechtsform_code))

                        strasse = address.get("street") or ""
                        hausnummer = address.get("houseNumber") or ""
                        plz = str(address.get("swissZipCode") or "")
                        ort = address.get("town") or ""
                        sitz = company.get("seat") or ort

                        cantons = meta.get("cantons") or []
                        kanton = cantons[0] if cantons else ""

                        zweck = commons.get("purpose") or ""

                        pub_date_raw = meta.get("publicationDate") or ""
                        pub_date = pub_date_raw[:10] if pub_date_raw else None

                        branche = ai_branche(zweck)

                        cur.execute(
                            """
                            INSERT INTO leads
                                (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                                 sitz, kanton, zweck, branche_ai, publikation_datum)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (uid) DO UPDATE SET
                                firma = EXCLUDED.firma,
                                rechtsform = EXCLUDED.rechtsform,
                                strasse = EXCLUDED.strasse,
                                hausnummer = EXCLUDED.hausnummer,
                                plz = EXCLUDED.plz,
                                ort = EXCLUDED.ort,
                                sitz = EXCLUDED.sitz,
                                kanton = EXCLUDED.kanton,
                                zweck = EXCLUDED.zweck,
                                branche_ai = EXCLUDED.branche_ai,
                                publikation_datum = EXCLUDED.publikation_datum
                            RETURNING (xmax = 0) AS inserted
                            """,
                            (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                             sitz, kanton, zweck, branche, pub_date)
                        )

                        row = cur.fetchone()
                        if row and row.get("inserted"):
                            inserted += 1
                        else:
                            updated += 1

                        if (inserted + updated) % 10 == 0:
                            conn.commit()

                    except Exception as exc:
                        logging.warning("ADMIN SYNC: Error processing publication: %s", exc)
                        errors += 1
                        continue

                conn.commit()

    except Exception as exc:
        logging.error("ADMIN SYNC: Database error: %s", exc)
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500

    logging.info("ADMIN SYNC: Completed - inserted=%d, updated=%d, errors=%d", inserted, updated, errors)

    return jsonify({
        "success": True,
        "datum_von": datum_von,
        "datum_bis": datum_bis,
        "total": len(publications),
        "inserted": inserted,
        "updated": updated,
        "errors": errors
    })


# ---------------------------- LEADS ----------------------------
@zevix_bp.route("/zevix/leads", methods=["GET"])
def get_leads():
    """
    Returns leads from the database with optional filtering.
    Requires a valid JWT token.
    Includes 'exported' flag per lead based on user's usage history for the current month.
    """
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    user_email = None
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_email = payload.get("email")
        except jwt.ExpiredSignatureError:
            return jsonify({"success": False, "error": "token_expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"success": False, "error": "invalid_token"}), 401

    if not user_email:
        user_email = session.get("email")

    if not user_email:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    datum_von = request.args.get("datum_von")
    datum_bis = request.args.get("datum_bis")
    kanton = request.args.get("kanton")
    branche = request.args.get("branche")
    try:
        limit = int(request.args.get("limit", 1000))
    except ValueError:
        limit = 1000
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    conditions = []
    params = []

    if datum_von:
        conditions.append("publikation_datum >= %s")
        params.append(datum_von)
    if datum_bis:
        conditions.append("publikation_datum <= %s")
        params.append(datum_bis)
    if kanton:
        conditions.append("kanton = %s")
        params.append(kanton.upper())
    if branche:
        conditions.append("branche_ai ILIKE %s")
        params.append(f"%{branche}%")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure_leads_table(cur)
                conn.commit()

                # Get user's exported lead IDs for current month
                month = get_month_key()
                cur.execute(
                    """
                    SELECT used_ids
                    FROM usage
                    WHERE user_email = %s AND month = %s
                    """,
                    (user_email, month),
                )
                usage_row = cur.fetchone()

                # Parse used_ids - handle both JSONB and string cases
                exported_ids_set = set()
                if usage_row:
                    used_ids_raw = usage_row.get("used_ids")
                    if isinstance(used_ids_raw, str):
                        try:
                            exported_ids_set = set(json.loads(used_ids_raw) if used_ids_raw else [])
                        except (json.JSONDecodeError, TypeError):
                            exported_ids_set = set()
                    elif isinstance(used_ids_raw, list):
                        exported_ids_set = set(used_ids_raw)

                query = f"""
                    SELECT id, uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                           sitz, kanton, zweck, branche_ai, publikation_datum, created_at
                    FROM leads
                    {where_clause}
                    ORDER BY publikation_datum DESC, id DESC
                    LIMIT %s OFFSET %s
                """
                params.extend([limit, offset])
                cur.execute(query, params)
                rows = cur.fetchall()

                leads = []
                for row in rows:
                    pub_date = row.get("publikation_datum")
                    created_at = row.get("created_at")
                    uid = row.get("uid") or ""
                    leads.append({
                        "id": row.get("id"),
                        "uid": uid,
                        "firma": row.get("firma"),
                        "rechtsform": row.get("rechtsform"),
                        "strasse": row.get("strasse"),
                        "hausnummer": row.get("hausnummer"),
                        "plz": row.get("plz"),
                        "ort": row.get("ort"),
                        "sitz": row.get("sitz"),
                        "kanton": row.get("kanton"),
                        "zweck": row.get("zweck"),
                        "branche_ai": row.get("branche_ai"),
                        "publikation_datum": pub_date.isoformat() if pub_date else None,
                        "created_at": created_at.isoformat() if created_at else None,
                        "exported": uid in exported_ids_set,
                    })

    except Exception as exc:
        logging.error("Fehler beim Laden der Leads: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({
        "success": True,
        "leads": leads,
        "count": len(leads),
        "limit": limit,
        "offset": offset,
    })
