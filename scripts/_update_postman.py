"""One-off helper to add the Reflection Room V1 folder to the Postman
collection without disturbing existing items. Idempotent: re-running drops
the old folder before re-injecting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COLLECTION = (
    Path(__file__).resolve().parents[1]
    / "Mirror-Collective-API.postman_collection.json"
)
FOLDER_NAME = "Reflection Room V1"


def _req(
    method: str, path_segments, *, body: dict | None = None, description: str = ""
):
    url_path = ["{{base_url}}", *path_segments]
    raw = "{{base_url}}/" + "/".join(path_segments)
    request: dict = {
        "method": method,
        "header": [
            {
                "key": "Authorization",
                "value": "Bearer {{access_token}}",
                "type": "text",
            },
            {"key": "Content-Type", "value": "application/json", "type": "text"},
        ],
        "url": {"raw": raw, "host": ["{{base_url}}"], "path": list(path_segments)},
        "description": description,
    }
    if body is not None:
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    return request


def _item(name: str, request: dict):
    return {"name": name, "request": request, "response": []}


def _build_folder() -> dict:
    items = [
        _item(
            "Submit Quiz",
            _req(
                "POST",
                ["api", "reflection", "quiz"],
                body={
                    "answers": {
                        "q1": "hopeful",
                        "q2": "inspiration",
                        "q3": "spiral",
                        "q4": "insight",
                    },
                    "session_id": None,
                    "user_override_tag": None,
                },
                description=("Score quiz, assign motif, seed loop state. Spec §6.1."),
            ),
        ),
        _item(
            "Override Room Skin",
            _req(
                "PUT",
                ["api", "me", "reflection", "room"],
                body={"motif_id": "mirror", "apply_to": "session"},
                description="Apply a room-skin override (spec §6.5).",
            ),
        ),
        _item(
            "Get Echo Snapshot",
            _req(
                "GET",
                ["api", "echo", "snapshot"],
                description="Return active loop state for the user's current session (spec §6.2).",
            ),
        ),
        _item(
            "Recommend Practice",
            _req(
                "POST",
                ["api", "echo", "recommend-practice"],
                body={
                    "session_id": "{{reflection_session_id}}",
                    "selected_loop": None,
                    "surface": "echo_signature",
                },
                description="Return one ranked practice (spec §6.3). 409 carries Retry-After.",
            ),
        ),
        _item(
            "Complete Practice",
            _req(
                "POST",
                ["api", "practice", "complete"],
                body={
                    "session_id": "{{reflection_session_id}}",
                    "loop_id": "pressure",
                    "tone_state": "rising",
                    "practice_id": "breath_4_6",
                    "rule_id": "pressure_loop_v1",
                    "helpful": True,
                },
                description="Log completion + optional helpful vote (spec §6.4). Returns refreshed snapshot inline.",
            ),
        ),
        _item(
            "Update Helpful (late)",
            _req(
                "PATCH",
                ["api", "practice", "complete", "{{completion_id}}", "helpful"],
                body={"helpful": True},
                description=(
                    "Late helpful vote (spec §6.6). NOTE: completion_id contains '#'. "
                    "Postman handles encoding via path segments — set the {{completion_id}} "
                    "variable to the raw completion_id string."
                ),
            ),
        ),
        _item(
            "Get Preferences",
            _req(
                "GET",
                ["api", "me", "preferences"],
                description="Read user flags + disallow_types.",
            ),
        ),
        _item(
            "Update Flags",
            _req(
                "PUT",
                ["api", "me", "preferences", "flags"],
                body={
                    "no_breathwork": True,
                    "reduced_motion": False,
                    "private_mode": False,
                },
                description="Partial update of user flags (spec §10.1).",
            ),
        ),
        _item(
            "Beacon: Private Mode Reveal",
            _req(
                "POST",
                ["api", "me", "private-mode", "reveal"],
                body={"surface": "echo_signature"},
                description="Telemetry beacon when user reveals private-mode content.",
            ),
        ),
        _item(
            "Beacon: Practice Expand",
            _req(
                "POST",
                ["api", "telemetry", "practice-expand"],
                body={"loop_id": "pressure", "practice_id": "breath_4_6"},
                description="Telemetry beacon when user opens a practice card back.",
            ),
        ),
        _item(
            "Beacon: Nudge Opened",
            _req(
                "POST",
                ["api", "telemetry", "nudge-opened"],
                body={"nudge_type": "morning_check_in"},
                description="Telemetry beacon when user expands a nudge.",
            ),
        ),
        _item(
            "Beacon: Echo Map Refresh",
            _req(
                "POST",
                ["api", "telemetry", "echo-map-refresh"],
                description="Telemetry beacon when user taps 'Update My Mirror'.",
            ),
        ),
        _item(
            "Dev Seed Loop State",
            _req(
                "POST",
                ["api", "dev", "echo", "loop-state"],
                body={
                    "loops": [
                        {
                            "loop_id": "pressure",
                            "tone_state": "rising",
                            "intensity_score": 0.74,
                        },
                        {
                            "loop_id": "grief",
                            "tone_state": "softening",
                            "intensity_score": 0.58,
                        },
                    ]
                },
                description="Dev-only QA seeder. Returns 404 in production.",
            ),
        ),
    ]
    return {
        "name": FOLDER_NAME,
        "description": (
            "Reflection Room V1 endpoints (quiz → motif → loop state → "
            "practice recommendation → completion). Spec lives at "
            "docs/reflection-room-v1/01_BACKEND_IMPLEMENTATION_SPEC.md."
        ),
        "item": items,
    }


def main() -> int:
    doc = json.loads(COLLECTION.read_text())
    # Drop any prior version of our folder (idempotent re-runs).
    doc["item"] = [i for i in doc["item"] if i.get("name") != FOLDER_NAME]
    doc["item"].append(_build_folder())
    COLLECTION.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Updated {COLLECTION}")
    print(f"  added {len(_build_folder()['item'])} requests under '{FOLDER_NAME}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
