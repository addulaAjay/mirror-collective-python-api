"""Load quiz questions from the bundled questions.json (no DynamoDB at runtime).

The archetype quiz is static V1 spec content, so the questions are baked into
the deployment instead of read from DynamoDB on every request. This removes the
cold-container ``table.scan()`` that made ``GET /quiz/questions`` (and the quiz
submit path) slow on Lambda cold starts.

The bundled file is the single source of truth at runtime. Keep it in sync with
the ``quiz_questions`` DynamoDB table via ``scripts/export_quiz_questions.py``.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

_QUESTIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "questions.json"


@lru_cache(maxsize=1)
def load_quiz_data() -> Dict[str, Any]:
    """Return the full parsed questions.json (questions + config + archetypes).

    Cached for the life of the process (the file is immutable at runtime). Use
    :func:`reset_cache` in tests that need to re-read after patching the file.
    """
    with open(_QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_quiz_questions() -> List[Dict[str, Any]]:
    """Return the list of quiz questions from the bundled file."""
    return load_quiz_data().get("questions", [])


def reset_cache() -> None:
    """Test-only: clear the cached file contents."""
    load_quiz_data.cache_clear()
