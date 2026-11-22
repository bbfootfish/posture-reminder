"""Reminder wording must match what the metric can actually observe.

`lean` is reached when the ear-shoulder-hip angle collapses but the shoulders
are not wide in frame.  Measured over 418 child-present samples, its head_fwd
median (140.2 deg) sits within 9.2 deg of `too_close` (131.0 deg) and its
head_up median is 0.487 -- the head is up.  The posture is a forward collapse,
not a sideways lean, so "两边坐平" tells the child to do something the camera
cannot even see here.  Wording is corrected; the classifier is unchanged.
"""

from __future__ import annotations

from posture.models import HEAD_DOWN, LEAN, TOO_CLOSE
from posture.phrases import PHRASES


def test_lean_phrases_ask_for_upright_posture_not_level_shoulders():
    texts = [p.text for p in PHRASES[LEAN]]
    joined = "".join(texts)
    assert "两边坐平" not in joined, (
        "sideways wording cannot be observed from this camera angle anyway"
    )
    # Every lean phrase should mention straightening up in some form.
    assert any(("坐直" in t) or ("直起" in t) or ("挺" in t) for t in texts), texts


def test_every_issue_keeps_exactly_two_variants():
    """Alternation between two wordings keeps a repeated reminder from nagging."""
    for issue, variants in PHRASES.items():
        assert len(variants) == 2, f"{issue} has {len(variants)} variants"


def test_all_phrase_files_are_distinct():
    files = [p.file for variants in PHRASES.values() for p in variants]
    assert len(files) == len(set(files)), f"duplicate phrase files: {files}"


def test_phrases_stay_short_for_a_child():
    """A reminder that takes more than ~3 s stops being a nudge."""
    for issue, variants in PHRASES.items():
        for p in variants:
            assert len(p.text) <= 10, f"{issue}: {p.text!r} is too long to speak quickly"


def test_phrases_are_kind_not_commands():
    """No scolding words: this device speaks to a 10-year-old all evening."""
    banned = ("不许", "别动", "错了", "不听话", "坐好点")
    for issue, variants in PHRASES.items():
        for p in variants:
            for word in banned:
                assert word not in p.text, f"{issue}: {p.text!r} contains {word!r}"
