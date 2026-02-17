# Stripe Email Synchronization Fix

## Problem
Users purchasing plans on Stripe with a different email than their app registration email caused plan synchronization to fail. Plans were synced to the Stripe checkout email instead of the user's app email.

## Solution
Implemented automatic email synchronization using the logged-in user's app email (`session["email"]`) for Stripe checkout creation.

## Changes Made

### 1. New Endpoint: `/zevix/create-checkout-session`

Creates a Stripe checkout session that automatically uses the authenticated user's app email.

**Request:**
```json
POST /zevix/create-checkout-session
Content-Type: application/json

{
  "price_id": "price_xxxxx",
  "success_url": "https://example.com/success?session_id={CHECKOUT_SESSION_ID}",
  "cancel_url": "https://example.com/cancel"
}
```

**Response (Success):**
```json
{
  "success": true,
  "session_id": "cs_test_xxxxx",
  "url": "https://checkout.stripe.com/pay/cs_test_xxxxx"
}
```

**Features:**
- ✅ Requires user to be logged in (`session["email"]` must be set)
- ✅ Automatically uses app email as `customer_email` in Stripe
- ✅ Stores app email in `metadata.app_email` (highest priority)
- ✅ Stores app email in `client_reference_id` (backup)
- ✅ Creates subscription-mode checkout sessions

### 2. Updated `resolve_email_from_checkout_session()`

Changed the email resolution priority to ensure app email is always found first:

**New Priority Order:**
1. **metadata.app_email** (highest priority - set by our endpoint)
2. **metadata.user_email** (backup metadata field)
3. **metadata.email** (alternative metadata field)
4. **client_reference_id** (often used for user identification)
5. **customer_email** (might be different from app email)
6. **customer_details.email** (customer details from Stripe)
7. **customer lookup** (last resort - API call to Stripe)

## Usage

### For Frontend Integration

Replace external Stripe Payment Links or checkout creation with the new endpoint:

```javascript
// User is already logged in (session["email"] is set on backend)
async function createCheckout(priceId) {
  const response = await fetch('/zevix/create-checkout-session', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include', // Important: send session cookies
    body: JSON.stringify({
      price_id: priceId,
      success_url: window.location.origin + '/success?session_id={CHECKOUT_SESSION_ID}',
      cancel_url: window.location.origin + '/cancel'
    })
  });
  
  const data = await response.json();
  
  if (data.success) {
    // Redirect to Stripe checkout
    window.location.href = data.url;
  } else {
    console.error('Checkout creation failed:', data.message);
  }
}

// Example: Create checkout for basic plan
createCheckout('price_basic_plan_id');
```

## Email Flow

### Before Fix ❌
1. User registers: `user1@test.com` (app email)
2. User buys plan on Stripe: `billing@company.com` (Stripe checkout email)
3. Plan syncs to: `billing@company.com` ❌
4. Result: User not found, plan not applied

### After Fix ✅
1. User registers: `user1@test.com` (app email)
2. User is logged in → `session["email"]` = `user1@test.com`
3. Frontend calls `/zevix/create-checkout-session` → automatically passes `customer_email = user1@test.com`
4. Stripe checkout created with app email in:
   - `customer_email`: `user1@test.com`
   - `metadata.app_email`: `user1@test.com`
   - `client_reference_id`: `user1@test.com`
5. User completes payment (even if they use a different email in payment form)
6. Plan syncs to: `user1@test.com` ✅
7. Result: Correct user found, plan applied successfully

## Testing

All tests pass successfully:

### Unit Tests (`test_stripe_email_fix.py`)
- ✅ Email normalization
- ✅ Metadata priority (app_email first)
- ✅ Fallback chain verification
- ✅ Invalid email handling
- ✅ Complete priority order

### Integration Tests (`test_integration.py`)
- ✅ Authenticated checkout creation
- ✅ Unauthenticated request rejection
- ✅ Parameter validation
- ✅ Error handling
- ✅ Correct email propagation

## Backward Compatibility

The changes are **fully backward compatible**:
- Existing `/zevix/verify-session` endpoint still works
- Old checkout sessions (without metadata) still resolve email through fallback chain
- No breaking changes to existing functionality

## Migration Guide

To migrate existing Stripe checkout creation:

1. **Ensure user is logged in** before allowing checkout creation
2. **Replace** direct Stripe API calls or Payment Links with `/zevix/create-checkout-session`
3. **Test** the complete flow: login → create checkout → complete payment → verify plan sync

## Security Considerations

- ✅ Requires authentication (checks `session["email"]`)
- ✅ User cannot specify arbitrary email (uses session email only)
- ✅ All Stripe communication uses secure API keys
- ✅ Proper error handling and logging
