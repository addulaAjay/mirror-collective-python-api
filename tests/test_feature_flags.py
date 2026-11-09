"""Server-driven feature flags.

Reflection Room needs to be invisible in production while testers exercise it
on the same binary. A build-time flag cannot do that — TestFlight and the App
Store ship the identical bundle — so the server decides per user and the client
renders accordingly.

Two levers, checked in this order:

  REFLECTION_ROOM_ENABLED=true        -> on for everyone (the prod switch)
  REFLECTION_ROOM_TESTER_IDS=a,b,c    -> on for those user ids only

Both are Lambda env vars, so flipping either takes effect on the next cold
start with no app release.

Fails CLOSED: anything unexpected — no user id, blank config, junk — hides the
feature. An unreleased feature leaking to production users is worse than a
tester having to wait for a redeploy.
"""

from unittest.mock import patch

from src.app.core import feature_flags as ff


class TestGlobalSwitch:
    def test_defaults_to_off(self, monkeypatch):
        # If this ever defaults on, an unfinished feature ships to everyone.
        assert ff.REFLECTION_ROOM_ENABLED is False

    def test_on_for_everyone_when_the_global_flag_is_set(self, monkeypatch):
        with patch.object(ff, "REFLECTION_ROOM_ENABLED", True):
            assert ff.reflection_room_enabled_for("anyone-at-all") is True

    def test_global_flag_does_not_need_a_user_id(self, monkeypatch):
        with patch.object(ff, "REFLECTION_ROOM_ENABLED", True):
            assert ff.reflection_room_enabled_for(None) is True


class TestTesterAllowlist:
    def test_a_listed_tester_sees_it(self, monkeypatch):
        monkeypatch.setenv("REFLECTION_ROOM_TESTER_IDS", "u-1")
        assert ff.reflection_room_enabled_for("u-1") is True

    def test_an_unlisted_user_does_not(self, monkeypatch):
        monkeypatch.setenv("REFLECTION_ROOM_TESTER_IDS", "u-1")
        assert ff.reflection_room_enabled_for("u-2") is False

    def test_empty_allowlist_hides_it_from_everyone(self, monkeypatch):
        monkeypatch.setenv("REFLECTION_ROOM_TESTER_IDS", "")
        monkeypatch.delenv("FEATURE_TESTER_IDS", raising=False)
        assert ff.reflection_room_enabled_for("u-1") is False

    def test_no_user_id_is_not_a_tester(self, monkeypatch):
        monkeypatch.setenv("REFLECTION_ROOM_TESTER_IDS", "u-1")
        assert ff.reflection_room_enabled_for(None) is False
        assert ff.reflection_room_enabled_for("") is False


class TestAllowlistParsing:
    def test_parses_a_comma_separated_list(self, monkeypatch):
        assert ff._parse_ids("a,b,c") == frozenset({"a", "b", "c"})

    def test_tolerates_whitespace_and_blanks(self, monkeypatch):
        # Env vars get hand-edited in the AWS console; "a, b, ,c," happens.
        assert ff._parse_ids("a, b, ,c,") == frozenset({"a", "b", "c"})

    def test_empty_config_is_an_empty_set_not_a_set_containing_empty(self, monkeypatch):
        # frozenset({""}) would match a user whose id failed to resolve.
        assert ff._parse_ids("") == frozenset()
        assert ff._parse_ids("   ") == frozenset()
        assert ff._parse_ids(None) == frozenset()


class TestFailsClosed:
    def test_both_levers_off_means_hidden(self, monkeypatch):
        with patch.object(ff, "REFLECTION_ROOM_ENABLED", False):
            monkeypatch.setenv("REFLECTION_ROOM_TESTER_IDS", "")
            monkeypatch.delenv("FEATURE_TESTER_IDS", raising=False)
            assert ff.reflection_room_enabled_for("u-1") is False

    def test_always_returns_a_real_bool(self, monkeypatch):
        # It is serialized straight into a JSON response; a truthy set or None
        # would leak internals to the client.
        monkeypatch.setenv("REFLECTION_ROOM_TESTER_IDS", "u-1")
        for uid in ["u-1", "u-2", None, ""]:
            assert isinstance(ff.reflection_room_enabled_for(uid), bool)


class TestSharedTesterAllowlist:
    """One list for every feature, with per-feature override.

    Two separate 8-UUID lists in provider.environment pushed
    UpdateFunctionConfiguration past its 5120-byte request limit and failed
    the production deploy on 2026-11-08
    (TranscodeCompleteLambdaFunction, HTTP 413). The shared list is declared
    once, on the api function only; the per-feature vars still work when set
    but are no longer declared in serverless.yml.
    """

    def test_shared_list_is_used_when_no_per_feature_list_is_set(self, monkeypatch):
        monkeypatch.setenv("FEATURE_TESTER_IDS", "shared-1,shared-2")
        monkeypatch.delenv("REFLECTION_ROOM_TESTER_IDS", raising=False)
        monkeypatch.delenv("ECHO_INBOX_TESTER_IDS", raising=False)

        assert ff._tester_ids("REFLECTION_ROOM") == frozenset({"shared-1", "shared-2"})
        assert ff._tester_ids("ECHO_INBOX") == frozenset({"shared-1", "shared-2"})

    def test_a_per_feature_list_overrides_the_shared_one(self, monkeypatch):
        monkeypatch.setenv("FEATURE_TESTER_IDS", "shared-1")
        monkeypatch.setenv("ECHO_INBOX_TESTER_IDS", "inbox-only")

        assert ff._tester_ids("ECHO_INBOX") == frozenset({"inbox-only"})
        assert ff._tester_ids("REFLECTION_ROOM") == frozenset({"shared-1"})

    def test_neither_set_falls_back_to_the_built_in_list(self, monkeypatch):
        # Changed deliberately on 2026-11-08. This used to resolve to an empty
        # set; now it falls back to _DEFAULT_TESTER_IDS, because the allowlist
        # no longer fits in the Lambda environment and lives in code instead.
        # Nothing in production sets either variable, so this IS the live path.
        monkeypatch.delenv("FEATURE_TESTER_IDS", raising=False)
        monkeypatch.delenv("ECHO_INBOX_TESTER_IDS", raising=False)

        assert ff._tester_ids("ECHO_INBOX") == ff._DEFAULT_TESTER_IDS

    def test_an_empty_per_feature_list_falls_back_rather_than_locking_out(
        self, monkeypatch
    ):
        # ECHO_INBOX_TESTER_IDS="" in the console should not silently mean
        # "nobody" while FEATURE_TESTER_IDS is populated.
        monkeypatch.setenv("FEATURE_TESTER_IDS", "shared-1")
        monkeypatch.setenv("ECHO_INBOX_TESTER_IDS", "")

        assert ff._tester_ids("ECHO_INBOX") == frozenset({"shared-1"})


class TestAllowlistLivesInCodeNotEnv:
    """The tester allowlist costs zero Lambda environment bytes.

    Production deploy, 2026-11-08, second failure:

        ApiLambdaFunction - environment variables exceeded the 4KB limit.
        Measured size: 4311 bytes

    Without the 313-byte FEATURE_TESTER_IDS the environment measures ~3998 —
    roughly 98 bytes under the ceiling. There is no room for it, and the
    22 DYNAMODB_* table-name vars account for ~1843 bytes of what is there.

    So the default allowlist lives in code. These are internal test-account
    ids, not secrets and not environment-specific config, so a constant is an
    honest home for them. The env override is kept for the day space frees
    up, but is no longer declared in serverless.yml.
    """

    def test_the_default_allowlist_is_not_empty(self, monkeypatch):
        # If this empties out, testers silently lose access with no error.
        monkeypatch.delenv("FEATURE_TESTER_IDS", raising=False)
        monkeypatch.delenv("ECHO_INBOX_TESTER_IDS", raising=False)

        assert len(ff._tester_ids("ECHO_INBOX")) > 0

    def test_known_tester_accounts_resolve(self, monkeypatch):
        monkeypatch.delenv("FEATURE_TESTER_IDS", raising=False)
        monkeypatch.delenv("REFLECTION_ROOM_TESTER_IDS", raising=False)

        # addula.ajay primary, and the mailtoajay44 trial account
        assert (
            ff.reflection_room_enabled_for("84289458-e081-70f8-a76f-859cc488fdde")
            is True
        )
        assert (
            ff.reflection_room_enabled_for("0428f478-e051-7025-1202-d3ab0994a2a4")
            is True
        )

    def test_a_stranger_still_does_not(self, monkeypatch):
        monkeypatch.delenv("FEATURE_TESTER_IDS", raising=False)

        assert ff.reflection_room_enabled_for("not-a-tester") is False

    def test_env_still_overrides_the_built_in_list(self, monkeypatch):
        # Kept working so the allowlist can move back to env if space frees up.
        monkeypatch.setenv("FEATURE_TESTER_IDS", "someone-else")

        assert ff.reflection_room_enabled_for("someone-else") is True
        assert (
            ff.reflection_room_enabled_for("84289458-e081-70f8-a76f-859cc488fdde")
            is False
        )
