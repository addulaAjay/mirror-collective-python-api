"""Export quiz questions from DynamoDB into the bundled questions.json.

The archetype quiz is served from src/app/data/questions.json at runtime (no
DynamoDB scan on the request path). This script keeps that file in sync with the
``quiz_questions`` table: it scans the table and rewrites ONLY the ``questions``
array, preserving the file's ``config``/``archetypes``/version metadata (which
live only in the file, not the table).

Usage:
    python scripts/export_quiz_questions.py            # write questions.json
    python scripts/export_quiz_questions.py --check     # exit 1 if out of sync

The --check mode writes nothing; use it in CI to fail the build when someone
edits the table without re-exporting (or vice-versa).
"""

import argparse
import decimal
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import boto3
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
TABLE_NAME = os.getenv(
    "DYNAMODB_QUIZ_QUESTIONS_TABLE",
    "mirror-collective-python-api-quiz-questions-production-v2",
)
QUESTIONS_FILE = (
    Path(__file__).resolve().parent.parent / "src" / "app" / "data" / "questions.json"
)


def _decimals_to_native(obj: Any) -> Any:
    """DynamoDB returns numbers as Decimal; convert to int/float for JSON."""
    if isinstance(obj, list):
        return [_decimals_to_native(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decimals_to_native(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def _normalize_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Stable shape/ordering for a question so diffs are minimal and reviewable."""
    options = [
        {k: opt[k] for k in ("text", "label", "image", "archetype") if k in opt}
        for opt in q.get("options", [])
    ]
    out: Dict[str, Any] = {
        "id": int(q["id"]),
        "question": q.get("question", ""),
        "type": q.get("type", "text"),
        "core": bool(q.get("core", False)),
        "options": options,
    }
    return out


def fetch_table_questions(table_name: str) -> List[Dict[str, Any]]:
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(table_name)
    items = _decimals_to_native(table.scan().get("Items", []))
    items.sort(key=lambda q: int(q["id"]))
    return [_normalize_question(q) for q in items]


def build_updated_file(table_questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge table questions into the existing file, preserving other keys."""
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["questions"] = table_questions
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if questions.json is out of sync with the table (no write).",
    )
    parser.add_argument("--table", default=TABLE_NAME, help="DynamoDB table name.")
    args = parser.parse_args()

    logger.info("Fetching questions from %s (%s)", args.table, AWS_REGION)
    table_questions = fetch_table_questions(args.table)
    updated = build_updated_file(table_questions)

    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        current = json.load(f)

    in_sync = current.get("questions") == updated["questions"]

    if args.check:
        if in_sync:
            logger.info("✅ questions.json is in sync with the table.")
            return 0
        logger.error(
            "❌ questions.json is OUT OF SYNC with the table. "
            "Run: python scripts/export_quiz_questions.py"
        )
        return 1

    if in_sync:
        logger.info("Already in sync; nothing to write.")
        return 0

    with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("✅ Wrote %d questions to %s", len(table_questions), QUESTIONS_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
