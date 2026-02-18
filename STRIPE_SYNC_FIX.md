# Stripe Subscription Sync Fix

## Problem
After purchasing a subscription via Stripe Payment Links, the plan was not correctly synchronized to the database. Users would see the plan in the frontend (localStorage), but the backend would still show `plan = "none"`.

## Root Causes

### 1. Bug in `apply_checkout_result_to_user()` - Wrong Table Updated
**File:** `routes/zevix.py` (Lines 184-194)

**Problem:** The function attempted to update the `usage` table instead of the `users` table. The `usage` table does not have a `plan` field, which would cause a PostgreSQL error.

**Database Schema:**
```sql
-- usage table structure (NO plan field!)
CREATE TABLE usage (
    user_email TEXT,
    month TEXT,
    used INTEGER,
    used_ids JSONB,
    PRIMARY KEY (user_email, month)
);

-- users table structure (HAS plan field)
CREATE TABLE users (
    email TEXT PRIMARY KEY,
    plan TEXT,
    valid_until BIGINT,
    ...
);
```

**Fix Applied:**
```python
# BEFORE (WRONG):
if old_plan != new_plan:
    month = get_month_key()
    cur.execute(
        """
        UPDATE usage          # ❌ usage table has NO plan field!
        SET plan = %s
        WHERE user_email = %s AND month = %s
        """,
        (new_plan, email, month),
    )
    conn.commit()

# AFTER (FIXED):
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
```

**Changes:**
- ✅ Update `users` table instead of `usage` table
- ✅ Set both `plan` and `valid_until` fields
- ✅ Invalidate cache immediately after update for instant sync

---

### 2. Bug in Cache Logic - Cached "none" Returned After Purchase
**File:** `routes/zevix.py` (Lines 286-292)

**Problem:** 
When a user logs in without a subscription, the system caches `plan = "none"`. If the user then purchases a subscription within the 5-minute cache TTL, the cached "none" value is returned, preventing the fresh Stripe lookup.

**Scenario:**
1. User logs in → Stripe sync runs → no subscription → Cache: `("none", timestamp)`
2. User purchases subscription 30 seconds later
3. Dashboard loads → Cache is still valid (TTL 5 min) → Returns cached `"none"` ❌
4. Stripe is NOT queried, even though subscription now exists!

**Condition Analysis:**
```python
# OLD LOGIC (PROBLEMATIC):
cached_plan = "none"
normalized_current_plan = "none"

if cached_plan != "none" or normalized_current_plan == "none":
    # Evaluates to: False or True → True
    return cached_plan  # ❌ Returns "none" from cache
```

**Fix Applied:**
```python
# BEFORE (PROBLEMATIC):
if not force:
    cached_plan, cache_hit = get_cached_stripe_plan(email)
    if cache_hit:
        # Return cached plan, but only if it's not a downgrade to "none"
        # (we never downgrade from a paid plan to none based on cache alone)
        if cached_plan != "none" or normalized_current_plan == "none":
            return cached_plan

# AFTER (FIXED):
if not force:
    cached_plan, cache_hit = get_cached_stripe_plan(email)
    if cache_hit:
        # Only use cache when BOTH plans are not "none" (paid → paid transition)
        # When plan is "none", always check Stripe fresh (user might have just purchased)
        if cached_plan != "none" and normalized_current_plan != "none":
            return cached_plan
        # When plan is "none", skip cache and let Stripe check run
```

**Changes:**
- ✅ Cache only used when BOTH plans are not "none" (paid → paid transitions)
- ✅ When either plan is "none", always perform fresh Stripe lookup
- ✅ Prevents stale "none" cache after purchase

---

### 3. Success Page Did Not Call Backend
**File:** `webflow-code-embeds-fixed/SUCCESS-FIXED.html` (New)

**Problem:**
The Webflow success page only updated localStorage but never called the backend. This meant `apply_checkout_result_to_user()` was never invoked, and the database was never updated.

**Old Flow (BROKEN):**
```
1. User pays on Stripe
2. Redirect to success page
3. Success page sets localStorage only  ❌
4. Dashboard loads
5. Backend still has plan = "none"  ❌
```

**Fix Applied:**
Created `SUCCESS-FIXED.html` that:
1. ✅ Calls `/zevix/verify-session` endpoint with session_id
2. ✅ Backend updates database via `apply_checkout_result_to_user()`
3. ✅ Backend invalidates cache
4. ✅ Frontend updates localStorage (backward compatibility)
5. ✅ Includes error handling and fallback

**New Flow (FIXED):**
```javascript
// Call backend to verify and sync
const res = await fetch("https://mandat-backend.onrender.com/zevix/verify-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ session_id: sessionId })
});

const data = await res.json();

if (data.success) {
    // Backend updated database ✅
    // Update localStorage for compatibility
    localStorage.setItem("auth_until", String(authUntil));
    localStorage.setItem("plan", PLAN);
    localStorage.setItem("stripe_session", sessionId);
    
    // Redirect to dashboard
    setTimeout(() => location.assign("/dashboard"), 1500);
}
```

---

## Expected Behavior After Fix

### Complete Flow:
1. **User clicks "Starten" on pricing page** → Stripe Payment Link
2. **User completes payment on Stripe**
3. **Stripe redirects to:** `https://www.zevix.ch/success?plan=basic&session_id=cs_xxxxx`
4. **Success page executes:**
   - ✅ Calls `/zevix/verify-session` endpoint
   - ✅ Backend: `apply_checkout_result_to_user()` updates `users` table → `plan = "basic"`, `valid_until = <30 days>`
   - ✅ Backend: Cache invalidated via `set_cached_stripe_plan()`
   - ✅ Frontend: localStorage updated (backward compatibility)
5. **User redirected to dashboard**
6. **Dashboard calls `/zevix/refresh-token`**
7. **Backend: `sync_user_plan_from_stripe()` runs:**
   - ✅ Cache is fresh with correct plan OR
   - ✅ Database already has correct plan → no Stripe call needed OR
   - ✅ If cache expired and plan is "none", fresh Stripe check runs
8. **User sees correct plan in dashboard** ✅

---

## Files Modified

### 1. `routes/zevix.py`
**Lines 184-196:** Fixed `apply_checkout_result_to_user()`
- Changed UPDATE from `usage` to `users` table
- Added `valid_until` field update
- Added cache invalidation

**Lines 287-295:** Fixed cache logic in `sync_user_plan_from_stripe()`
- Changed condition to only use cache when both plans are not "none"
- Ensures fresh Stripe check when plan might have just been purchased

### 2. `webflow-code-embeds-fixed/SUCCESS-FIXED.html` (New)
- Added backend call to `/zevix/verify-session`
- Maintained localStorage updates for compatibility
- Added comprehensive error handling
- Added fallback behavior for network errors

### 3. `STRIPE_SYNC_FIX.md` (This File)
- Complete documentation of all three bugs
- Detailed explanation of fixes
- Flow diagrams
- Testing checklist

---

## Testing Checklist

### Pre-Purchase Testing
- [ ] User registers account → Database shows `plan = "none"`
- [ ] User logs in → Dashboard shows "Kein Abo" / "No Subscription"
- [ ] Pricing page "Starten" buttons work (redirect to Stripe Payment Links)

### Purchase Flow Testing
- [ ] User clicks "Starten" → Stripe checkout opens
- [ ] User completes payment with test card
- [ ] Stripe redirects to success page with `session_id` and `plan` parameters
- [ ] Success page calls backend (check browser DevTools Network tab)
- [ ] Backend responds with `{"success": true, ...}`

### Post-Purchase Verification
- [ ] **Database Check:** Run `SELECT email, plan, valid_until FROM users WHERE email = '<user_email>'`
  - Expected: `plan = "basic"` (or purchased plan), `valid_until` set to ~30 days from now
- [ ] **Dashboard Check:** User redirected to dashboard, sees correct plan name and limits
- [ ] **Lead Export:** User can export leads (respects plan limits: Basic=500, Business=1000, Enterprise=4500)
- [ ] **Cache Check:** Subsequent logins use cached plan (no unnecessary Stripe calls)

### Edge Cases
- [ ] User purchases subscription while cache is still valid (should work now!)
- [ ] User purchases subscription, logs out, logs back in → Still sees subscription
- [ ] Network error during success page backend call → Fallback still redirects to dashboard
- [ ] Multiple rapid page refreshes on success page → Deduplication prevents duplicate API calls

---

## Deployment Notes

### Backend Deployment (Automatic)
- Changes to `routes/zevix.py` deploy automatically via GitHub → Render
- No manual intervention needed

### Frontend Deployment (Manual - Webflow)
1. Open Webflow project
2. Navigate to Success page
3. Open Custom Code section (Page Settings → Custom Code → Before `</body>` tag)
4. Replace existing code with contents of `webflow-code-embeds-fixed/SUCCESS-FIXED.html`
5. Publish site

### Verification After Deployment
1. Test with Stripe test mode first
2. Use test card: `4242 4242 4242 4242`, any future expiry, any CVC
3. Verify database updates correctly
4. Test with production Payment Links
5. Monitor logs for any errors

---

## Related Documentation
- [STRIPE_EMAIL_FIX.md](./STRIPE_EMAIL_FIX.md) - Previous fix for email synchronization issues
- [Backend Status](./BACKEND-STATUS.md) - Overall backend status and endpoints
- [Webflow Integration Guide](./webflow-code-embeds-fixed/README.md) - Frontend integration documentation

---

## Rollback Plan

If issues occur after deployment:

### Backend Rollback
```bash
git revert <commit_hash>
git push origin main
# Render will auto-deploy previous version
```

### Frontend Rollback (Webflow)
1. Remove backend call from success page
2. Keep only localStorage updates
3. Publish site

**Note:** This will revert to the broken state where plan is not synced to database. Only use if critical production issue occurs.

---

## Support

For issues or questions:
- Check Render logs: https://dashboard.render.com/
- Check database: `SELECT * FROM users WHERE email = '<email>'`
- Check Stripe Dashboard: https://dashboard.stripe.com/
- Review this documentation

**Common Issues:**
- "Plan still shows 'none'" → Check if success page code is deployed to Webflow
- "Payment successful but no database update" → Check backend logs for errors in `apply_checkout_result_to_user()`
- "Cache still returning old value" → Should not happen with new logic, but can force refresh with `force=True` parameter
