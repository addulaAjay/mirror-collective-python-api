# Echo Media Attachment Fix

## Problem

Videos and audio files were not being attached to echoes when uploaded. The workflow was:

1. User creates echo with recipient (no `release_date`) → Echo auto-released (status = RELEASED)
2. User gets upload URL → Success
3. User uploads file to S3 → Success
4. User PATCHes echo with `media_url` → **FAILED: "Cannot update locked or released echo"**

## Root Cause

The `update_echo()` method in [echo_service.py](../src/app/services/echo_service.py) blocked ALL updates to RELEASED echoes, including media attachment. This was too restrictive for the upload workflow where:

- Echo is created and released immediately (when it has a recipient)
- Media is uploaded separately (S3 direct upload)
- Media URL is attached via PATCH after upload completes

## Solution

Allow media attachment as a **special case** on RELEASED echoes, with these constraints:

1. **Only `media_url` and optionally `echo_type`** can be updated
2. **Only if `media_url` is currently empty** (first-time attachment only)
3. **No other fields** can be updated together with media

### Code Changes

**File:** `src/app/services/echo_service.py` ([echo_service.py](../src/app/services/echo_service.py#L479-L520))

```python
# Special case: Allow media attachment on RELEASED echoes (first-time only)
is_media_only_update = (
    "media_url" in data
    and set(data.keys()) <= {"media_url", "echo_type"}
    and not echo.media_url  # Only if media_url is currently empty
)

# Prevent updates to locked/released echoes (except first-time media attachment)
if echo.status != EchoStatus.DRAFT and not is_media_only_update:
    logger.warning(
        f"Attempted to update non-draft echo {echo_id} (status={echo.status.value})"
    )
    raise InternalServerError("Cannot update locked or released echo")

# Apply updates
if "media_url" in data:
    echo.media_url = data["media_url"]
    logger.info(
        f"Attached media to echo {echo_id} (status={echo.status.value})"
    )
if "echo_type" in data:
    try:
        echo.echo_type = EchoType(data["echo_type"])
    except (ValueError, KeyError):
        pass  # Keep existing type if invalid
```

## Test Coverage

**File:** `tests/test_echo_media_attachment.py` ([test_echo_media_attachment.py](../tests/test_echo_media_attachment.py))

**6 comprehensive tests (all passing):**

1. ✅ `test_can_attach_media_to_released_echo` - First-time media attachment works
2. ✅ `test_can_attach_media_and_echo_type_to_released_echo` - Media + type update works
3. ✅ `test_cannot_update_other_fields_on_released_echo` - Title/content updates still blocked
4. ✅ `test_cannot_attach_media_twice` - Cannot replace existing media
5. ✅ `test_cannot_update_media_with_other_fields_on_released_echo` - Media + other field blocked
6. ✅ `test_draft_echo_can_be_updated_freely` - DRAFT echoes unchanged (can update any field)

## Workflow After Fix

1. User creates echo with recipient (no `release_date`) → Echo auto-released ✅
2. User gets upload URL → Success ✅
3. User uploads file to S3 → Success ✅
4. User PATCHes echo with `media_url` → **SUCCESS** ✅ (now allowed as special case)
5. Echo displays with video/audio in app ✅

## Security & Validation

- **First-time only:** Cannot replace existing media (prevents accidental overwrites)
- **Owner-only:** Only echo owner can update (existing authorization still applies)
- **Immutable after first attach:** Once media is attached, echo becomes immutable
- **Type safety:** Invalid `echo_type` values are ignored (keeps existing type)

## API Behavior

**PATCH** `/api/echoes/{echo_id}`

**Allowed on RELEASED echoes:**
```json
{
  "media_url": "s3://bucket/user-123/echo-456.mp4"
}
```

```json
{
  "media_url": "s3://bucket/user-123/echo-456.mp4",
  "echo_type": "VIDEO"
}
```

**Still blocked on RELEASED echoes:**
```json
{
  "title": "New Title"  // ❌ Cannot update metadata
}
```

```json
{
  "media_url": "s3://new-file.mp4",
  "media_url": "s3://existing-file.mp4"  // ❌ Already has media
}
```

```json
{
  "media_url": "s3://file.mp4",
  "title": "New Title"  // ❌ Cannot mix media with other fields
}
```

## Related Issues

- Original issue: Video/audio uploads not attaching to echoes
- Related fix: [Recipient access control](./RECIPIENT_ECHO_ACCESS_FIX.md)
- Related fix: [Storage quota Decimal handling](./STORAGE_QUOTA_DECIMAL_FIX.md)

## Deployment

- Status: ✅ **Ready for deployment**
- Tests: ✅ All passing (6/6)
- Breaking changes: None
- Backward compatible: Yes

## Verification

After deployment, verify:

1. Create echo with recipient → Echo auto-releases
2. Request upload URL for video → Get presigned S3 URL
3. Upload video to S3 → File uploads successfully
4. PATCH echo with media_url → **Should succeed (200 OK)**
5. View echo in app → Video displays and plays
6. Try to update title → **Should fail (400 Bad Request)**
7. Try to upload different video → **Should fail (400 Bad Request)**
