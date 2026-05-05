"""Phase 1 — config loader unit tests.

Covers Phase 1.2-1.5 exit criteria:
  * each loader parses successfully
  * key counts match spec (6 rules, 17 practices, 18 tone entries, 11 motifs)
  * cross-file invariants (rule candidates resolve in catalog, etc.)
  * ConfigLoadError on bad config
"""

from __future__ import annotations

import pytest

from src.app.core.exceptions import ConfigLoadError, MotifNotFound
from src.app.services.echo.tone_library_loader import load_tone_library
from src.app.services.practice.catalog_loader import load_practice_catalog
from src.app.services.practice.personalization_loader import (
    load_personalization_defaults,
)
from src.app.services.practice.rule_loader import load_practice_rules
from src.app.services.practice.settings_loader import load_micro_practice_settings
from src.app.services.reflection import _config_io
from src.app.services.reflection.motif_mapping_loader import load_motif_mapping
from src.app.services.reflection.quiz_rules_loader import load_quiz_rules
from src.app.services.reflection.quiz_to_loop_seeding_loader import (
    SUPPORTED_LOOPS,
    SUPPORTED_TONES,
    load_quiz_to_loop_seeding,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(autouse=True)
def _reset_loader_caches():
    """Each test starts with cold caches so env-var overrides take effect."""
    _config_io.clear_all_loader_caches()
    yield
    _config_io.clear_all_loader_caches()


# ============================================================
# quiz_rules_loader
# ============================================================


class TestQuizRulesLoader:
    def test_loads_all_4_questions(self):
        rules = load_quiz_rules()
        assert set(rules.questions.keys()) == {"q1", "q2", "q3", "q4"}

    def test_weights_match_spec(self):
        # Spec §4.1: q1=1, q2=2, q3=2, q4=1
        rules = load_quiz_rules()
        assert rules.weights == {"q1": 1, "q2": 2, "q3": 2, "q4": 1}

    def test_default_tz_is_new_york(self):
        rules = load_quiz_rules()
        assert rules.session.default_tz == "America/New_York"

    def test_tie_break_use_q3_true(self):
        rules = load_quiz_rules()
        assert rules.tie_break.use_q3 is True
        assert rules.tie_break.allow_user_override is True

    def test_q3_has_11_answers(self):
        # The 11 Q3 symbols correspond 1:1 to the 11 motif tags.
        rules = load_quiz_rules()
        assert len(rules.questions["q3"].answers) == 11

    def test_missing_file_raises_config_load_error(self, monkeypatch):
        monkeypatch.setenv(
            "REFLECTION_QUIZ_RULES_PATH",
            "src/app/data/reflection/does_not_exist.yaml",
        )
        _config_io.clear_all_loader_caches()
        with pytest.raises(ConfigLoadError, match="not found"):
            load_quiz_rules()

    def test_malformed_yaml_raises_config_load_error(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: [valid: yaml: at: all\n")
        monkeypatch.setenv("REFLECTION_QUIZ_RULES_PATH", str(bad))
        _config_io.clear_all_loader_caches()
        with pytest.raises(ConfigLoadError):
            load_quiz_rules()

    def test_missing_q3_raises_config_load_error(self, tmp_path, monkeypatch):
        bad = tmp_path / "missing_q3.yaml"
        bad.write_text(
            "version: 1\n"
            "weights: {q1: 1, q2: 2, q3: 2, q4: 1}\n"
            "questions:\n"
            "  q1: {prompt: x, answers: {curious: [direction]}}\n"
            "  q2: {prompt: x, answers: {clarity: [clarity]}}\n"
            "  q4: {prompt: x, answers: {soothing: [reflection]}}\n"
        )
        monkeypatch.setenv("REFLECTION_QUIZ_RULES_PATH", str(bad))
        _config_io.clear_all_loader_caches()
        with pytest.raises(ConfigLoadError, match="missing keys"):
            load_quiz_rules()


# ============================================================
# motif_mapping_loader
# ============================================================


class TestMotifMappingLoader:
    def test_11_motifs(self):
        mapping = load_motif_mapping()
        assert len(mapping.motifs) == 11

    def test_evolution_maps_to_spiral(self):
        # Spec §4.2 names this entry explicitly.
        entry = load_motif_mapping().lookup("evolution")
        assert entry.motif_id == "spiral"
        assert entry.motif_name == "Spiral"
        assert entry.room_skin == "Spiral Room"

    def test_lookup_unknown_tag_raises(self):
        with pytest.raises(MotifNotFound):
            load_motif_mapping().lookup("not_a_tag")

    def test_lookup_by_motif_id(self):
        entry = load_motif_mapping().lookup_by_motif_id("compass")
        assert entry.motif_name == "Compass"

    def test_lookup_by_unknown_motif_id_raises(self):
        with pytest.raises(MotifNotFound):
            load_motif_mapping().lookup_by_motif_id("banana")

    def test_motif_ids_are_unique(self):
        mapping = load_motif_mapping()
        ids = [e.motif_id for e in mapping.all_entries()]
        assert len(set(ids)) == len(ids)

    def test_every_quiz_tag_has_motif_row(self):
        rules = load_quiz_rules()
        mapping = load_motif_mapping()
        all_quiz_tags = {
            tag
            for q in rules.questions.values()
            for tags in q.answers.values()
            for tag in tags
        }
        for tag in all_quiz_tags:
            mapping.lookup(tag)  # raises if missing


# ============================================================
# quiz_to_loop_seeding_loader
# ============================================================


class TestQuizToLoopSeedingLoader:
    def test_top_n_3_min_seed_score_0_45(self):
        cfg = load_quiz_to_loop_seeding().config
        assert cfg.top_n == 3
        assert cfg.min_seed_score == 0.45

    def test_intensity_floor_ceiling(self):
        cfg = load_quiz_to_loop_seeding().config
        assert cfg.intensity_floor == 0.50
        assert cfg.intensity_ceiling == 0.85

    def test_q3_weight_highest(self):
        seeding = load_quiz_to_loop_seeding()
        weights = {q: c.weight for q, c in seeding.contributions.items()}
        assert weights["q3"] >= max(weights["q1"], weights["q2"], weights["q4"])

    def test_grounded_seeds_no_loops(self):
        # Spec §4.8: "grounded" is a settled state and intentionally seeds nothing.
        seeding = load_quiz_to_loop_seeding()
        assert seeding.contributions["q1"].answers["grounded"] == []

    def test_all_contributions_use_v1_loops_and_tones(self):
        seeding = load_quiz_to_loop_seeding()
        for q, qc in seeding.contributions.items():
            for ans, contribs in qc.answers.items():
                for c in contribs:
                    assert c.loop in SUPPORTED_LOOPS, (q, ans, c.loop)
                    assert c.tone in SUPPORTED_TONES, (q, ans, c.tone)

    def test_unsupported_loop_in_config_raises(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad_seeding.yaml"
        bad.write_text(
            "version: 1\n"
            "config: {top_n: 3, min_seed_score: 0.45, intensity_floor: 0.5,"
            " intensity_ceiling: 0.85, tone_tiebreak_priority: [rising]}\n"
            "contributions:\n"
            "  q1:\n"
            "    weight: 1.0\n"
            "    answers:\n"
            "      curious:\n"
            "        - {loop: clarity, tone: rising, score: 0.5}\n"
            "  q2: {weight: 0.7, answers: {}}\n"
            "  q3: {weight: 1.5, answers: {}}\n"
            "  q4: {weight: 0.7, answers: {}}\n"
        )
        monkeypatch.setenv("REFLECTION_QUIZ_TO_LOOP_SEEDING_PATH", str(bad))
        _config_io.clear_all_loader_caches()
        with pytest.raises(ConfigLoadError, match="Unsupported loop"):
            load_quiz_to_loop_seeding()


# ============================================================
# tone_library_loader
# ============================================================


class TestToneLibraryLoader:
    def test_6_loops_18_pairs(self):
        lib = load_tone_library()
        assert len(lib.loops) == 6
        total_pairs = sum(len(b.tones) for b in lib.loops.values())
        assert total_pairs == 18  # 6 loops × 3 tone states

    def test_lookup_returns_icon_and_line(self):
        lib = load_tone_library()
        entry = lib.lookup("pressure", "rising")
        assert entry.icon == "🔺"
        assert "Pressure" in entry.reflection_line

    def test_lookup_missing_loop_raises_keyerror(self):
        lib = load_tone_library()
        with pytest.raises(KeyError):
            lib.lookup("not_a_loop", "rising")

    def test_lookup_missing_tone_raises_keyerror(self):
        lib = load_tone_library()
        with pytest.raises(KeyError):
            lib.lookup("pressure", "not_a_tone")

    def test_label_for(self):
        assert load_tone_library().label_for("self_silencing") == "Self-Silencing"

    def test_missing_loop_in_config_raises(self, tmp_path, monkeypatch):
        bad = tmp_path / "incomplete_tone.yaml"
        # Only 3 of 6 loops present.
        bad.write_text(
            "version: 1\n"
            "loops:\n"
            "  pressure:\n"
            "    icon: 🔺\n"
            "    label: Pressure\n"
            "    tones:\n"
            "      rising: {reflection_line: x}\n"
            "      steady: {reflection_line: x}\n"
            "      softening: {reflection_line: x}\n"
            "  overwhelm:\n"
            "    icon: 🌊\n"
            "    label: Overwhelm\n"
            "    tones:\n"
            "      rising: {reflection_line: x}\n"
            "      steady: {reflection_line: x}\n"
            "      softening: {reflection_line: x}\n"
            "  grief:\n"
            "    icon: 🌿\n"
            "    label: Grief\n"
            "    tones:\n"
            "      rising: {reflection_line: x}\n"
            "      steady: {reflection_line: x}\n"
            "      softening: {reflection_line: x}\n"
        )
        monkeypatch.setenv("REFLECTION_TONE_LIBRARY_PATH", str(bad))
        _config_io.clear_all_loader_caches()
        with pytest.raises(ConfigLoadError, match="missing loops"):
            load_tone_library()


# ============================================================
# practice_rule_loader
# ============================================================


class TestPracticeRuleLoader:
    def test_6_rules(self):
        assert len(load_practice_rules().rules) == 6

    def test_rule_ids_unique(self):
        rules = load_practice_rules().rules
        ids = [r.id for r in rules]
        assert len(set(ids)) == len(ids)

    def test_rules_cover_all_v1_loops(self):
        rule_loops = {r.when.loop_id for r in load_practice_rules().rules}
        assert rule_loops == set(SUPPORTED_LOOPS)

    def test_fallback_defaults(self):
        fb = load_practice_rules().fallback
        assert fb.enabled is True
        assert fb.default_practice_id == "breath_4_6"
        assert fb.alternate_for_no_breathwork_id == "name_and_need"
        assert fb.rule_id == "fallback"

    def test_grief_rule_has_no_min_strength(self):
        # Spec §4.4: grief_softening_v1 only gates on tone, not strength.
        rule = load_practice_rules().rule_by_id("grief_softening_v1")
        assert rule is not None
        assert rule.when.min_strength is None
        assert rule.when.trend_in == ["softening"]

    def test_pressure_rule_min_strength_0_60(self):
        rule = load_practice_rules().rule_by_id("pressure_loop_v1")
        assert rule is not None
        assert rule.when.min_strength == 0.60

    def test_transition_rule_recent_days_max_3(self):
        rule = load_practice_rules().rule_by_id("transition_bridge_v1")
        assert rule is not None
        assert rule.when.recent_days_max == 3


# ============================================================
# practice_catalog_loader
# ============================================================


class TestPracticeCatalogLoader:
    def test_17_practices(self):
        assert len(load_practice_catalog().practices) == 17

    def test_ids_unique(self):
        catalog = load_practice_catalog()
        ids = catalog.all_ids()
        assert len(set(ids)) == len(ids)

    def test_get_known_practice(self):
        practice = load_practice_catalog().get("breath_4_6")
        assert practice.title == "Ease Pressure"
        assert practice.type == "breath"
        assert practice.duration_sec == 120

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError):
            load_practice_catalog().get("not_a_practice")

    def test_three_breath_practices_typed_correctly(self):
        # Spec §4.5 type table: breath_4_6 + breath_box_4 are 'breath'.
        catalog = load_practice_catalog()
        for pid in ("breath_4_6", "breath_box_4"):
            assert catalog.get(pid).type == "breath"

    def test_every_rule_candidate_resolves(self):
        catalog = load_practice_catalog()
        rules = load_practice_rules()
        for r in rules.rules:
            for cid in r.candidates:
                catalog.get(cid)  # raises KeyError if missing
        # Fallback IDs too:
        catalog.get(rules.fallback.default_practice_id)
        catalog.get(rules.fallback.alternate_for_no_breathwork_id)


# ============================================================
# settings_loader
# ============================================================


class TestSettingsLoader:
    def test_default_cooldowns(self):
        s = load_micro_practice_settings()
        assert s.defaults.cooldown_hours_default == 12
        assert s.defaults.cooldown_hours_grief == 24

    def test_fallback_enabled(self):
        s = load_micro_practice_settings()
        assert s.defaults.fallback_enabled is True

    def test_max_practices_per_session(self):
        s = load_micro_practice_settings()
        assert s.defaults.max_practices_per_session == 3


# ============================================================
# personalization_loader
# ============================================================


class TestPersonalizationLoader:
    def test_weights_match_spec(self):
        pd = load_personalization_defaults()
        assert pd.weights.helpful_vote == 2.0
        assert pd.weights.not_helpful_vote == -2.0
        assert pd.weights.time_of_day_match == 0.5
        assert pd.weights.recent_use_penalty == -1.0

    def test_decay_half_life_21_days(self):
        pd = load_personalization_defaults()
        assert pd.decay.recency_decay_half_life_days == 21.0

    def test_user_flags_default_false(self):
        pd = load_personalization_defaults()
        assert pd.user_flags_default.no_breathwork is False
        assert pd.user_flags_default.reduced_motion is False
        assert pd.user_flags_default.private_mode is False

    def test_4_time_of_day_buckets_no_underscore_keys(self):
        pd = load_personalization_defaults()
        assert set(pd.time_of_day_buckets.keys()) == {
            "morning",
            "midday",
            "evening",
            "night",
        }
        assert all(not k.startswith("_") for k in pd.time_of_day_buckets.keys())

    def test_global_field_aliased_correctly(self):
        # 'global' is a Python keyword; loader must use Field(alias="global").
        pd = load_personalization_defaults()
        assert pd.global_config.disallow_types == []
