"""Unit tests for safety_filter (spec §B.2.6 + §9.3)."""

from __future__ import annotations

import pytest

from src.app.models.user_personalization import UserFlags, UserPersonalization
from src.app.services.practice.catalog_loader import load_practice_catalog
from src.app.services.practice.safety_filter import apply


@pytest.fixture
def catalog_ids():
    return load_practice_catalog()


def _practices(catalog, ids):
    return [catalog.get(i) for i in ids]


def test_no_breathwork_removes_breath_practices(catalog_ids):
    candidates = _practices(catalog_ids, ["breath_4_6", "name_and_need"])
    prefs = UserPersonalization(user_id="u1", flags=UserFlags(no_breathwork=True))
    out = apply(candidates, prefs)
    assert {p.id for p in out} == {"name_and_need"}


def test_disallow_types_removes_typed_practices(catalog_ids):
    # heart_hand_breath is type=somatic
    candidates = _practices(
        catalog_ids, ["breath_4_6", "heart_hand_breath", "name_and_need"]
    )
    prefs = UserPersonalization(user_id="u1", disallow_types=["somatic"])
    out = apply(candidates, prefs)
    assert {p.id for p in out} == {"breath_4_6", "name_and_need"}


def test_global_disallow_removes_typed_practices(catalog_ids):
    # one_percent_first_call is type=action
    candidates = _practices(catalog_ids, ["breath_4_6", "one_percent_first_call"])
    prefs = UserPersonalization(user_id="u1")
    out = apply(candidates, prefs, global_disallow_types=["action"])
    assert {p.id for p in out} == {"breath_4_6"}


def test_no_filter_preserves_all_candidates(catalog_ids):
    candidates = _practices(
        catalog_ids, ["breath_4_6", "name_and_need", "heart_hand_breath"]
    )
    prefs = UserPersonalization(user_id="u1")
    out = apply(candidates, prefs)
    assert {p.id for p in out} == {"breath_4_6", "name_and_need", "heart_hand_breath"}


def test_combined_user_and_global_disallow(catalog_ids):
    candidates = _practices(
        catalog_ids, ["breath_4_6", "heart_hand_breath", "one_percent_first_call"]
    )
    prefs = UserPersonalization(
        user_id="u1",
        flags=UserFlags(no_breathwork=True),
        disallow_types=["somatic"],
    )
    out = apply(candidates, prefs, global_disallow_types=["action"])
    assert out == []
