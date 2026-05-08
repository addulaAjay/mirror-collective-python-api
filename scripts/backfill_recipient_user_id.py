"""Backfill recipient_user_id on legacy recipient rows.

For every recipient row in DynamoDB where recipient_user_id is missing or empty,
look up the email in the users table; if a UserProfile exists for that email,
patch recipient_user_id to the user's Cognito sub.

This is needed because:

1. recipient_user_id is only set at recipient-creation time (echo_service.create_recipient).
2. Recipients added before the recipient signed up have recipient_user_id = None.
3. The inbox query keys off recipient-user-id-index, so unlinked rows are invisible.

The signup flow now back-links new users automatically (auth_controller.confirm_email),
but historical rows need this one-shot script.

Usage (from repo root):
    AWS_PROFILE=... python scripts/backfill_recipient_user_id.py
    AWS_PROFILE=... python scripts/backfill_recipient_user_id.py --dry-run

Environment variables (read from src.app.services config — same as the API):
    DYNAMODB_RECIPIENTS_TABLE
    DYNAMODB_USERS_TABLE
    AWS_REGION

Idempotent: rerunning is safe. Rows already linked are skipped.

Exit codes:
    0  Completed (with summary)
    1  Fatal error before scan finished
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Ensure we can import src.app.* when run from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("backfill_recipient_user_id")


async def _run(dry_run: bool) -> int:
    from src.app.models.echo import _current_timestamp  # type: ignore
    from src.app.services.dynamodb_service import DynamoDBService
    from src.app.services.echo_service import EchoService

    echo_service = EchoService()
    dynamodb_service = DynamoDBService()

    scanned = 0
    already_linked = 0
    linked = 0
    no_user = 0
    deleted_skipped = 0
    errors = 0

    async with echo_service.session.resource(
        "dynamodb", **echo_service._get_dynamodb_kwargs()
    ) as dynamodb:
        table = await dynamodb.Table(echo_service.recipients_table)

        scan_kwargs: dict = {}
        while True:
            response = await table.scan(**scan_kwargs)
            for item in response.get("Items", []):
                scanned += 1

                if item.get("deleted_at") is not None:
                    deleted_skipped += 1
                    continue

                if item.get("recipient_user_id"):
                    already_linked += 1
                    continue

                email = (item.get("email") or "").strip().lower()
                if not email:
                    no_user += 1
                    continue

                try:
                    user = await dynamodb_service.get_user_by_email(email)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    logger.error(
                        f"get_user_by_email failed for {email} "
                        f"(recipient {item.get('recipient_id')}): {e}"
                    )
                    continue

                if not user:
                    no_user += 1
                    continue

                user_id = user.user_id
                recipient_id = item["recipient_id"]

                if dry_run:
                    logger.info(
                        f"[dry-run] would link recipient {recipient_id} "
                        f"({email}) -> user {user_id}"
                    )
                    linked += 1
                    continue

                try:
                    await table.update_item(
                        Key={"recipient_id": recipient_id},
                        UpdateExpression=(
                            "SET recipient_user_id = :uid, updated_at = :ts"
                        ),
                        ExpressionAttributeValues={
                            ":uid": user_id,
                            ":ts": _current_timestamp(),
                        },
                    )
                    linked += 1
                    logger.info(
                        f"linked recipient {recipient_id} ({email}) -> "
                        f"user {user_id}"
                    )
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    logger.error(
                        f"update_item failed for recipient {recipient_id}: {e}"
                    )

            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

    logger.info("---- Backfill summary ----")
    logger.info(f"scanned          : {scanned}")
    logger.info(f"already linked   : {already_linked}")
    logger.info(f"newly linked     : {linked}{' (dry-run)' if dry_run else ''}")
    logger.info(f"no matching user : {no_user}")
    logger.info(f"deleted skipped  : {deleted_skipped}")
    logger.info(f"errors           : {errors}")
    return 0 if errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be linked without writing.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_run(dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
