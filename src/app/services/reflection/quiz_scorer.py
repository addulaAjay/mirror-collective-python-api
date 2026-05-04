"""Quiz scoring engine (spec §7).

Pure function: ``answers + rules + optional override → ScoringResult``. No I/O,
no DDB. The result includes the winning tag, scores per tag, an explanation
list, and whether the FE should prompt the user to break a tie.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

from ...core.exceptions import ConfigLoadError, InvalidQuizAnswer, OverrideTagNotInTie
from .quiz_rules_loader import QuizRules


@dataclass(frozen=True)
class ScoringResult:
    """Output of ``score_quiz``.

    ``override_allowed=True`` means the quiz produced a tie not resolved by Q3 —
    FE should render the override chooser. The ``tied_tags`` field carries
    the candidates the FE shows.
    """

    winning_tag: str
    override_allowed: bool
    scores: Dict[str, int]
    explanation: List[str]
    tied_tags: List[str]  # populated when override_allowed=True


def score_quiz(
    answers: Dict[str, str],
    rules: QuizRules,
    user_override_tag: Optional[str] = None,
) -> ScoringResult:
    """Run the spec §7 algorithm.

    Raises:
        InvalidQuizAnswer: an answer is not in the rules' answer set.
        ConfigLoadError: scoring produces an empty bucket (config bug).
        OverrideTagNotInTie: ``user_override_tag`` provided but not in the tied set.
    """
    # 1. Accumulate weighted tag buckets.
    buckets: Dict[str, int] = defaultdict(int)
    for q in ("q1", "q2", "q3", "q4"):
        if q not in answers:
            raise InvalidQuizAnswer(f"missing answer for {q}")
        weight = rules.weights[q]
        answer_value = answers[q]
        question = rules.questions[q]
        if answer_value not in question.answers:
            raise InvalidQuizAnswer(
                f"invalid answer for {q}: {answer_value!r}; "
                f"expected one of {sorted(question.answers.keys())}"
            )
        for tag in question.answers[answer_value]:
            buckets[tag] += weight

    if not buckets:
        # Possible only with broken config (every q's answers list is empty).
        raise ConfigLoadError("quiz scoring produced empty buckets")

    # 4. Determine winners at max_score.
    max_score = max(buckets.values())
    winners = sorted([tag for tag, s in buckets.items() if s == max_score])
    explanation = _build_explanation(answers, rules)

    if len(winners) == 1:
        return ScoringResult(
            winning_tag=winners[0],
            override_allowed=False,
            scores=dict(buckets),
            explanation=explanation,
            tied_tags=[],
        )

    # 5. Tie-break #1 — Q3 alignment.
    if rules.tie_break.use_q3:
        q3_tags = set(rules.questions["q3"].answers[answers["q3"]])
        q3_winners = [w for w in winners if w in q3_tags]
        if len(q3_winners) == 1:
            return ScoringResult(
                winning_tag=q3_winners[0],
                override_allowed=False,
                scores=dict(buckets),
                explanation=explanation,
                tied_tags=[],
            )

    # 6. Tie-break #2 — user override or deterministic-default-with-override-allowed.
    if user_override_tag is not None:
        if user_override_tag not in winners:
            raise OverrideTagNotInTie(
                f"override tag '{user_override_tag}' not in tied set {winners}"
            )
        return ScoringResult(
            winning_tag=user_override_tag,
            override_allowed=False,  # user has resolved it
            scores=dict(buckets),
            explanation=explanation,
            tied_tags=[],
        )

    # No override given → return deterministic default with override_allowed=True
    # so the FE can prompt the user.
    if not rules.tie_break.allow_user_override:
        # Config disabled override entirely — fall back to alphabetical default.
        return ScoringResult(
            winning_tag=winners[0],
            override_allowed=False,
            scores=dict(buckets),
            explanation=explanation,
            tied_tags=[],
        )
    return ScoringResult(
        winning_tag=winners[0],
        override_allowed=True,
        scores=dict(buckets),
        explanation=explanation,
        tied_tags=winners,
    )


def _build_explanation(answers: Dict[str, str], rules: QuizRules) -> List[str]:
    """Format: ``"Q{n}={ans} (×{w} → {tag1}, {tag2})"``."""
    out: List[str] = []
    for q in ("q1", "q2", "q3", "q4"):
        ans = answers[q]
        weight = rules.weights[q]
        tags = rules.questions[q].answers[ans]
        joined = ", ".join(tags) if tags else "(no tags)"
        out.append(f"{q.upper()}={ans} (×{weight} → {joined})")
    return out
