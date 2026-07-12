"""Unit tests for the LifeAnchorStructurer gpt-4o-mini pass (Phase 2B)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.app.services.life_anchor_structurer import LifeAnchorStructurer

pytestmark = pytest.mark.asyncio


def _structurer(raw=None, *, raises=False) -> LifeAnchorStructurer:
    svc = MagicMock()
    if raises:
        svc.send_with_overrides_async = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        svc.send_with_overrides_async = AsyncMock(return_value=raw)
    return LifeAnchorStructurer(svc)


class TestStructure:
    async def test_valid_json(self):
        raw = json.dumps(
            {
                "anchor_type": "loss",
                "title": "User's wife passed away",
                "relationship": "wife",
                "emotional_weight": "sacred",
                "tone_guidance": ["Do not say time heals everything."],
            }
        )
        out = await _structurer(raw).structure("my wife died")
        assert out == {
            "anchor_type": "loss",
            "title": "User's wife passed away",
            "relationship": "wife",
            "emotional_weight": "sacred",
            "tone_guidance": ["Do not say time heals everything."],
        }

    async def test_strips_code_fences(self):
        raw = (
            "```json\n"
            + json.dumps(
                {"anchor_type": "custom", "title": "A milestone", "tone_guidance": []}
            )
            + "\n```"
        )
        out = await _structurer(raw).structure("something")
        assert out is not None
        assert out["title"] == "A milestone"

    async def test_invalid_anchor_type_coerced_to_custom(self):
        raw = json.dumps({"anchor_type": "banana", "title": "X"})
        out = await _structurer(raw).structure("x")
        assert out is not None
        assert out["anchor_type"] == "custom"
        assert out["emotional_weight"] == "medium"  # invalid/missing → default

    async def test_missing_title_returns_none(self):
        raw = json.dumps({"anchor_type": "loss"})
        assert await _structurer(raw).structure("x") is None

    async def test_malformed_json_returns_none(self):
        assert await _structurer("not json at all").structure("x") is None

    async def test_openai_error_returns_none(self):
        assert await _structurer(raises=True).structure("x") is None

    async def test_empty_candidate_returns_none(self):
        assert await _structurer("{}").structure("   ") is None
