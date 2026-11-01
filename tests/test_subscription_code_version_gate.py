"""The `subscription_required` code may only be sent to clients that can survive it.

Background. A 403 carrying ``{"code": "subscription_required"}`` makes the app
navigate to the paywall. Clients before the 2026-11-01 fix ALSO fire an
``Alert.alert`` in the same tick, and presenting an alert mid-transition is a
hard crash on iOS. That was the 2026-10-11 production incident.

The stopgap was to stop sending the code at all
(``ECHO_VAULT_EMIT_SUBSCRIPTION_CODE=false``), which stopped the crash but also
killed paywall routing: a lapsed user now gets a plain error and no way to
subscribe.

Turning the flag back on globally is not safe, because every already-shipped
client would start crashing again. So the emission is gated on the client
telling us what it is, via the ``X-App-Version`` header.

The gate FAILS CLOSED on every ambiguity — missing header, unparseable header,
no request at all. A client that cannot prove it is new is treated as old. The
cost of a false negative is a worse error message; the cost of a false positive
is a crash.
"""

from types import SimpleNamespace
from unittest.mock import patch

from src.app.core import entitlements as ent


def a_request(app_version=None):
    """Minimal stand-in for a Starlette Request — only headers are read."""
    headers = {}
    if app_version is not None:
        headers["x-app-version"] = app_version
    return SimpleNamespace(headers=headers)


class TestVersionGate:
    """With the master flag ON, the client version decides."""

    def test_a_new_client_gets_the_machine_readable_code(self):
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            detail = ent.subscription_required_detail(a_request("1.1.0"))

        assert isinstance(detail, dict)
        assert detail["code"] == "subscription_required"

    def test_a_newer_client_also_gets_it(self):
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            detail = ent.subscription_required_detail(a_request("1.2.3"))

        assert isinstance(detail, dict)

    def test_an_old_client_gets_the_plain_string(self):
        # 1.0.1 is what crashed in production.
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            detail = ent.subscription_required_detail(a_request("1.0.1"))

        assert isinstance(detail, str)
        assert "subscription_required" not in detail

    def test_a_client_that_sends_no_version_header_is_treated_as_old(self):
        # Every client shipped before this change sends no header at all.
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            detail = ent.subscription_required_detail(a_request())

        assert isinstance(detail, str)

    def test_no_request_at_all_is_treated_as_old(self):
        # Internal callers, or a call site that forgets to pass the request:
        # fail closed rather than assume.
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            detail = ent.subscription_required_detail()

        assert isinstance(detail, str)


class TestVersionParsing:
    def test_compares_numerically_not_lexically(self):
        # "1.10.0" > "1.9.0" numerically, but "1.10.0" < "1.9.0" as strings.
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            with patch.object(ent, "MIN_CLIENT_FOR_SUBSCRIPTION_CODE", "1.9.0"):
                assert isinstance(
                    ent.subscription_required_detail(a_request("1.10.0")), dict
                )

    def test_a_build_suffix_does_not_break_parsing(self):
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            detail = ent.subscription_required_detail(a_request("1.1.0 (34)"))

        assert isinstance(detail, dict)

    def test_two_component_versions_work(self):
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            with patch.object(ent, "MIN_CLIENT_FOR_SUBSCRIPTION_CODE", "1.1"):
                assert isinstance(
                    ent.subscription_required_detail(a_request("1.1")), dict
                )

    def test_garbage_fails_closed(self):
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            for junk in ["", "   ", "not-a-version", "???", "v"]:
                detail = ent.subscription_required_detail(a_request(junk))
                assert isinstance(detail, str), f"{junk!r} should fail closed"

    def test_a_hostile_header_cannot_crash_the_guard(self):
        # The header is attacker-controlled. Worst case is a plain string.
        with patch.object(ent, "EMIT_SUBSCRIPTION_CODE", True):
            for junk in ["9" * 500, "1.1.0; DROP TABLE", "\x00\x01", "-1.-1"]:
                detail = ent.subscription_required_detail(a_request(junk))
                assert isinstance(detail, (str, dict))


class TestMasterFlagStillWins:
    """The env flag remains a kill switch — version alone is not enough."""

    def test_flag_off_means_plain_string_even_for_a_new_client(self):
        assert ent.EMIT_SUBSCRIPTION_CODE is False  # current production state

        detail = ent.subscription_required_detail(a_request("99.0.0"))

        assert isinstance(detail, str)

    def test_flag_off_is_still_the_default(self):
        # If this flips to True by default, every old client starts crashing
        # the moment it also passes the version gate.
        assert ent.EMIT_SUBSCRIPTION_CODE is False
