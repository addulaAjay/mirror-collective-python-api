# Storage Quota Decimal Type Fix

## Issue
Voice and video uploads were failing with the error:
```
ERROR - Error checking quota for user: unsupported operand type(s) for /: 'float' and 'decimal.Decimal'
```

This prevented users from uploading media files to their echoes.

## Root Cause
DynamoDB returns numeric values as `Decimal` objects (from the `boto3` library), but Python arithmetic operations require consistent types. The storage quota service was attempting to perform division between:
- `float` values (from `calculate_user_storage_usage()`)
- `Decimal` values (from DynamoDB fields like `user_profile.echo_vault_quota_gb`)

Python does not allow direct arithmetic operations between `float` and `Decimal` types without explicit conversion, causing a `TypeError`.

## Affected Code Locations

### 1. `check_quota_exceeded()` - Line 129
**Problem:**
```python
quota_gb = user_profile.echo_vault_quota_gb  # Returns Decimal from DynamoDB
percent_used = (current_usage / quota_gb * 100)  # current_usage is float
# TypeError: unsupported operand type(s) for /: 'float' and 'decimal.Decimal'
```

**Fix:**
```python
# Convert Decimal to float for arithmetic operations
quota_gb = float(user_profile.echo_vault_quota_gb) if user_profile.echo_vault_quota_gb else 0.0
percent_used = (current_usage / quota_gb * 100) if quota_gb > 0 else 0
```

### 2. `can_upload()` - Line 169
**Problem:**
```python
projected_usage = quota_status["usage_gb"] + file_size_gb
# TypeError if quota_status["usage_gb"] is Decimal
```

**Fix:**
```python
# Ensure usage_gb is float for arithmetic
current_usage_gb = float(quota_status["usage_gb"]) if isinstance(quota_status["usage_gb"], Decimal) else quota_status["usage_gb"]
projected_usage = current_usage_gb + file_size_gb
```

## Changes Made

### 1. Added Import
```python
from decimal import Decimal
```

### 2. Type Conversion in `check_quota_exceeded()`
- Convert `user_profile.echo_vault_quota_gb` from `Decimal` to `float` before arithmetic
- Added null safety check

### 3. Type Conversion in `can_upload()`
- Check if `quota_status["usage_gb"]` is `Decimal` and convert to `float`
- Safe handling for both `Decimal` and `float` types

## Testing
Created comprehensive test suite in `tests/test_storage_quota_decimal_fix.py`:

1. **test_check_quota_handles_decimal_values**: Verifies quota checking with `Decimal` values from DynamoDB
2. **test_can_upload_handles_decimal_values**: Verifies upload permission checking with `Decimal` values
3. **test_can_upload_rejects_when_quota_exceeded_with_decimals**: Verifies rejection logic with `Decimal` values
4. **test_check_quota_with_zero_quota**: Verifies zero division protection

All tests pass ✅

## Files Modified
- `src/app/services/storage_quota_service.py` - Fixed type conversion issues
- `tests/test_storage_quota_decimal_fix.py` - New test file with 4 comprehensive tests

## Impact
After deploying this fix:
- ✅ Users can upload voice and video files to echo
- ✅ Quota checking works correctly with DynamoDB `Decimal` values
- ✅ No type errors during arithmetic operations
- ✅ Proper handling of edge cases (zero quota, missing values)

## Prevention
To avoid similar issues in the future when working with DynamoDB:
1. Always convert `Decimal` values to `float` before arithmetic operations
2. Use `isinstance(value, Decimal)` checks when type is uncertain
3. Add defensive null/zero checks for division operations
4. Test with actual DynamoDB data (which uses `Decimal`) not just mock data

## Related Documentation
- Python Decimal documentation: https://docs.python.org/3/library/decimal.html
- boto3 DynamoDB number handling: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/customizations/dynamodb.html#valid-dynamodb-types
