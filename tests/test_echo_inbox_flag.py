"""The Echo Inbox is built but switched off — this is the switch.

The whole vault-to-vault path works: recipients link to accounts by Cognito
sub, ``get_echoes_received`` queries the recipient-user-id GSI, and
``EchoInboxScreen`` exists and is routed. But the app's only navigation path
to it (``EchoVaultLibraryScreen.tsx:608``) is wrapped in a literal
``{false && (...)}`` — compiled out of the bundle, "on hold pending
design/product discussion".

Meanwhile the delivery email branches on ``is_registered`` to tell registered
recipients to open the echo in the app, which they currently cannot do.

Replacing that hard-coded false with a server-driven flag lets the inbox be
opened to testers first, then everyone, without an App Store release — the
same shape Reflection Room uses. A build-time flag would not work here: the
app's ``process.env`` flags are never substituted (react-native-dotenv is a
dependency but was never added to babel.config.js), and TestFlight and the App
Store ship the identical binary anyway.
"""

from types import SimpleNamespace  # noqa: F401 - parity with sibling tests
from unittest.mock import patch

from src.app.core import feature_flags as ff


class TestEchoInboxFlag:
    def test_defaults_to_off(self):
        # The feature is on product hold. If this ever defaults on, the inbox
        # appears for everyone without that hold being resolved.
        assert ff.ECHO_INBOX_ENABLED is False

    def test_hidden_by_default_for_an_ordinary_user(self):
        assert ff.echo_inbox_enabled_for("someone") is False

    def test_global_switch_opens_it_to_everyone(self):
        with patch.object(ff, "ECHO_INBOX_ENABLED", True):
            assert ff.echo_inbox_enabled_for("anyone") is True

    def test_global_switch_needs_no_user_id(self):
        with patch.object(ff, "ECHO_INBOX_ENABLED", True):
            assert ff.echo_inbox_enabled_for(None) is True

    def test_a_listed_tester_sees_it(self):
        with patch.object(ff, "ECHO_INBOX_TESTER_IDS", frozenset({"u-1"})):
            assert ff.echo_inbox_enabled_for("u-1") is True

    def test_an_unlisted_user_does_not(self):
        with patch.object(ff, "ECHO_INBOX_TESTER_IDS", frozenset({"u-1"})):
            assert ff.echo_inbox_enabled_for("u-2") is False

    def test_no_user_id_is_not_a_tester(self):
        with patch.object(ff, "ECHO_INBOX_TESTER_IDS", frozenset({"u-1"})):
            assert ff.echo_inbox_enabled_for(None) is False
            assert ff.echo_inbox_enabled_for("") is False

    def test_returns_a_real_bool(self):
        # Serialized straight into a JSON response.
        with patch.object(ff, "ECHO_INBOX_TESTER_IDS", frozenset({"u-1"})):
            for uid in ["u-1", "u-2", None, ""]:
                assert isinstance(ff.echo_inbox_enabled_for(uid), bool)


class TestFlagsAreIndependent:
    """Reflection Room and Echo Inbox must not share a switch."""

    def test_reflection_room_on_does_not_open_the_inbox(self):
        with patch.object(ff, "REFLECTION_ROOM_ENABLED", True):
            assert ff.echo_inbox_enabled_for("u-1") is False

    def test_inbox_on_does_not_open_reflection_room(self):
        with patch.object(ff, "ECHO_INBOX_ENABLED", True):
            assert ff.reflection_room_enabled_for("u-1") is False

    def test_separate_allowlists(self):
        with patch.object(ff, "ECHO_INBOX_TESTER_IDS", frozenset({"inbox-u"})):
            with patch.object(
                ff, "REFLECTION_ROOM_TESTER_IDS", frozenset({"reflect-u"})
            ):
                assert ff.echo_inbox_enabled_for("inbox-u") is True
                assert ff.echo_inbox_enabled_for("reflect-u") is False
                assert ff.reflection_room_enabled_for("reflect-u") is True
                assert ff.reflection_room_enabled_for("inbox-u") is False
