"""
How an image turn is persisted as text (Phase 2, D4).

The image bytes are never stored. What IS stored is the user's caption plus a
one-line description of the photo, composed into the message ``content``. That
composed text is what later turns replay into the prompt, and what
``archetype_engine.analyze_message`` (pure text regex) reads — so if this
composition is wrong, image turns silently stop contributing to the 5-signal
analysis.

A useful side effect: the composed content is always non-empty, even for a
no-caption turn. That means ``conversation_service._validate_message_inputs``
(which requires non-empty content) needs no change.
"""

from src.app.services.chat_attachment_service import compose_attachment_turn_content


class TestComposition:
    def test_caption_and_description_are_both_kept(self):
        result = compose_attachment_turn_content(
            message="what do you see?",
            image_description="a quiet lake at dusk",
        )

        assert "what do you see?" in result
        assert "a quiet lake at dusk" in result

    def test_description_is_marked_as_an_attachment_not_as_user_words(self):
        # The model must not mistake our description for something the user
        # typed. The bracketed marker is what distinguishes them.
        result = compose_attachment_turn_content(
            message="thoughts?",
            image_description="a lake",
        )

        assert "[shared a photo: a lake]" in result
        assert result.startswith("thoughts?")

    def test_a_no_caption_turn_still_produces_non_empty_content(self):
        # This is what lets _validate_message_inputs stay untouched.
        result = compose_attachment_turn_content(
            message="",
            image_description="a lake at dusk",
        )

        assert result.strip()
        assert "a lake at dusk" in result

    def test_a_whitespace_caption_is_treated_as_no_caption(self):
        result = compose_attachment_turn_content(
            message="   ",
            image_description="a lake",
        )

        assert result == "[shared a photo: a lake]"

    def test_falls_back_to_a_bare_marker_when_there_is_no_description(self):
        # The model ignored JSON mode, or vision failed. Record that a photo
        # was shared without inventing detail about it.
        result = compose_attachment_turn_content(message="look", image_description="")

        assert result == "look\n\n[shared a photo]"

    def test_no_caption_and_no_description_still_records_the_photo(self):
        result = compose_attachment_turn_content(message="", image_description="")

        assert result == "[shared a photo]"

    def test_strips_surrounding_whitespace_from_the_description(self):
        result = compose_attachment_turn_content(
            message="", image_description="  a lake  "
        )

        assert result == "[shared a photo: a lake]"

    def test_collapses_a_multiline_description_onto_one_line(self):
        # A description with newlines would corrupt the transcript format the
        # summarizer builds (it joins turns with newlines).
        result = compose_attachment_turn_content(
            message="", image_description="a lake\nat dusk\n"
        )

        assert "\n" not in result.replace("\n\n", "")
        assert "a lake at dusk" in result

    def test_truncates_an_overlong_description(self):
        # Guards the token budget: this text is replayed into every subsequent
        # prompt for the next 10 turns.
        result = compose_attachment_turn_content(
            message="", image_description="x" * 2000
        )

        assert len(result) < 500

    def test_result_contains_no_base64(self):
        # Sanity: the composer takes no bytes, so it cannot leak any. Pinned
        # because this string is persisted and replayed.
        result = compose_attachment_turn_content(
            message="look", image_description="a lake"
        )

        assert "base64" not in result
        assert "data:" not in result
