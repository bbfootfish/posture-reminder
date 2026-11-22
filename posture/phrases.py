"""Fixed phrase book.

The six lines are pre-generated MP3 files; the runtime never synthesises
speech.  The filenames are the contract with the static HTTP server that
serves ``/opt/posture-media`` on the PVE host.
"""

from __future__ import annotations

from urllib.parse import quote

from .models import HEAD_DOWN, LEAN, TOO_CLOSE, Phrase

PHRASES: dict[str, tuple[Phrase, Phrase]] = {
    TOO_CLOSE: (
        Phrase(text="离本子远一点点", file="too_close_1.mp3"),
        Phrase(text="往后退一点点", file="too_close_2.mp3"),
    ),
    HEAD_DOWN: (
        Phrase(text="头抬高一点点", file="head_down_1.mp3"),
        Phrase(text="把背直起来一点点", file="head_down_2.mp3"),
    ),
    # `lean` fires on a forward collapse (ear-shoulder-hip angle dropped) while
    # the shoulders stay narrow in frame -- the head is still up, so it is *not*
    # head_down.  Measured: head_fwd median 140.2 deg vs 131.0 for too_close
    # (9.2 deg apart, same posture family), head_up median 0.487.
    # The old "两边坐平" asked for level shoulders, which this rear-quarter camera
    # cannot even observe.  The wording now targets the posture actually seen.
    LEAN: (
        Phrase(text="坐正一下", file="lean_1.mp3"),
        Phrase(text="身体直起来", file="lean_2.mp3"),
    ),
}


def phrase_url(media_base_url: str, phrase: Phrase) -> str:
    """Absolute URL of a pre-generated phrase, or ``""`` if no base is set."""
    base = (media_base_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{quote(phrase.file)}"
