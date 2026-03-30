# Recipient Echo Access Fix

## Issue
Recipients were unable to view echoes sent to them. When a user tried to view an echo where they were the recipient (not the owner), they received a "404 Echo not found" error.

## Root Cause
The `get_echo()` method in `echo_service.py` only checked if the requesting user was the **owner** of the echo:

```python
# OLD CODE (BROKEN)
if echo.user_id != user_id:
    logger.warning(f"User {user_id} attempted to access echo {echo_id} owned by {echo.user_id}")
    return None
```

This meant:
- Owner user A creates echo and sends it to recipient B
- Recipient B (who has `recipient_user_id` linked to their account) tries to view the echo
- Access denied because B is not the owner → 404 error

## Solution
Modified `get_echo()` to allow both **owners** AND **recipients** to view echoes:

```python
# NEW CODE (FIXED)
is_owner = echo.user_id == user_id
is_recipient = False

# Check if user is the recipient
if not is_owner and echo.recipient_id:
    recipient = await self.get_recipient(echo.recipient_id, echo.user_id)
    if recipient and recipient.recipient_user_id == user_id:
        is_recipient = True

if not is_owner and not is_recipient:
    return None  # Deny access
```

### Access Control Logic
1. **Owner access**: User owns the echo (`echo.user_id == user_id`) → Allow
2. **Recipient access**:
   - Echo has a `recipient_id`
   - Fetch the recipient record
   - Check if `recipient.recipient_user_id == user_id` → Allow
3. **No access**: User is neither owner nor recipient → Deny (404)

## Additional Improvements

### 1. Optimization: Recipient Caching
The fix caches the recipient lookup to avoid duplicate DynamoDB calls:

```python
cached_recipient = None

# Check recipient access
if not is_owner and echo.recipient_id:
    recipient = await self.get_recipient(echo.recipient_id, echo.user_id)
    if recipient and recipient.recipient_user_id == user_id:
        is_recipient = True
        cached_recipient = recipient  # Cache it

# Later: Enrich echo with recipient details
recipient = cached_recipient or await self.get_recipient(...)  # Use cache
```

This avoids making two identical queries when a recipient views an echo.

### 2. Bug Fix: Recipient Enrichment
Fixed recipient enrichment to always fetch from the **echo owner's** recipients:

```python
# OLD (BROKEN when recipient views):
recipient = await self.get_recipient(echo.recipient_id, user_id)

# NEW (CORRECT):
recipient = await self.get_recipient(echo.recipient_id, echo.user_id)
```

The `user_id` parameter specifies whose recipient list to query. When a recipient views an echo, `user_id` is the recipient's ID, not the owner's. This would fail to find the recipient record since it belongs to the owner.

## Testing
Created comprehensive tests in `tests/test_recipient_access.py`:

1. **test_recipient_can_view_echo_sent_to_them**: Verifies recipients with linked accounts can view echoes
2. **test_recipient_without_user_account_cannot_view_echo**: Verifies unlinked recipients (no `recipient_user_id`) cannot view echoes

Both tests pass ✅

## Files Modified
- `src/app/services/echo_service.py` - Updated `get_echo()` method (lines 237-267)
- `tests/test_recipient_access.py` - New test file with 2 comprehensive tests

## Logs
When a recipient successfully accesses an echo, you'll see:
```
User {recipient_user_id} accessing echo {echo_id} as recipient (recipient_id: {recipient_id})
```

When access is denied:
```
User {user_id} attempted to access echo {echo_id} owned by {owner_id} - not owner or recipient
```

## Deployment
After deploying this fix, recipients will be able to:
- View echoes sent to them in their inbox
- Open and read echo details
- See media content (with signed URLs)

The fix maintains security:
- Only owners and intended recipients can access echoes
- Other users receive 404 (not 403 to avoid leaking echo existence)
- Recipient access requires `recipient_user_id` linking (user must have an account)
