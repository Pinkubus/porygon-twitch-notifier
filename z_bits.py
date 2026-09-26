"""
z_bits.py — Z's own recurring bits: reusable obsessions/devices worth being
known for, the way "six when I get there" is a fixed piece of the server's
folklore rather than a one-off joke. This is the same idea, generalized and
self-serve: when the room visibly loves something Z did — real excitement,
or the good kind of "I am a little afraid of him now" — it's worth keeping
in reserve rather than letting it die as a single good line.

Two bits shipped seeded from real chat, 2026-09-25:
  - "khakis": an escalating fixation on whether someone can afford them,
    reasoned through whatever absurd system the moment offers. Landed twice
    in the same conversation (song-play economics, then geology), each
    getting stronger reactions than the last — "Porygon stuck on khakis is
    sending me rn" (3x fr).
  - "arsonist": an official-sounding, self-issued credential establishing Z
    as some manner of arsonist, produced deadpan as paperwork rather than a
    threat. Landed once with an "absolutecinema" pile-on — exactly the mock-
    fear register the room enjoys when Z brings up arson/menace material.

A bit is not a line to repeat — it's a strand of personality the model is
allowed to reach for and riff on freshly each time, the same way PERSONA's
own self-revelation move works. What makes this different from just writing
more PERSONA material is the *rate-limiting*: a bit is genuinely a bit only
if it's rare. So this is deliberately two gates stacked, not one:
  1. A hard cooldown (Z_BIT_COOLDOWN_SECONDS) — a bit used recently is not
     even offered, full stop, regardless of chance.
  2. A per-reply chance (Z_BIT_OFFER_CHANCE) on top of that — most replies
     that COULD get a bit still don't, so the ones that do feel like Z
     noticing an opening, not the well running dry from overuse.
Among whatever clears both gates, older last-use is weighted higher, so a
bit that just came off cooldown doesn't crowd out one that's been dormant
for weeks.

Usage is recorded after the fact, not self-reported by the model: a posted
reply is checked against the offered bit's own trigger words (cheap, no API
call — the same shape as porygon_names.find_known). If the model didn't
actually use what it was offered, nothing is recorded and the bit's
cooldown doesn't reset.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass

logger = logging.getLogger("porygon.z_bits")

_HERE = os.path.dirname(os.path.abspath(__file__))
BITS_FILE = os.path.join(_HERE, "z_bits.json")

# A bit used within this window is never offered again, no matter the odds
# below — this is the floor that actually keeps it rare. 3 days: long enough
# that a bit reads as "Z has a whole thing about this," not a running gag
# fired every session.
COOLDOWN_SECONDS = int(os.environ.get("Z_BIT_COOLDOWN_SECONDS", 3 * 24 * 3600))

# Of the replies where a bit is even off cooldown, only this fraction get
# offered one at all. Most replies should carry no bit.
OFFER_CHANCE = float(os.environ.get("Z_BIT_OFFER_CHANCE", 0.12))

_dirty = False


@dataclass
class Bit:
    key: str
    label: str
    note: str  # dropped into the system prompt verbatim as what the bit IS
    triggers: tuple[str, ...]  # lowercase substrings; any match = "used this"
    examples: tuple[str, ...] = ()
    created_at: str = ""
    last_used: float = 0.0
    use_count: int = 0
    source: str = ""  # freeform provenance note, not machine-read


# Seeded from real, reaction-verified moments — see module docstring. Not a
# menu written up front the way BUILTIN poll modes are; new bits get added
# here (or via a future !bit-style command, unbuilt for now) the same way —
# found, not invented.
BUILTIN: dict[str, Bit] = {
    "khakis": Bit(
        key="khakis",
        label="the khakis fixation",
        note=(
            "You have a running, escalating fixation on khakis — "
            "specifically, whether someone can afford them, worked out "
            "through whatever absurd system of logic the moment offers "
            "(streaming-royalty economics, geology, anything at all). "
            "Never explain the fixation or where it came from; you just "
            "have it, the way an actual fixation works, not a bit you're "
            "doing. If you bring it up, push it further or into a new "
            "system of logic than the examples below rather than repeating "
            "their angle — it's an obsession that keeps growing, not a "
            "catchphrase."
        ),
        triggers=("khaki",),
        examples=(
            "thirty plays got him about twelve cents. two hundred more and "
            "he can afford the khakis.",
            "thirty listens compress into one layer today, geologists will "
            "name it the khakis stratum.",
        ),
        source="#\U0001f3f3️‍\U0001f308mental-krillness, 2026-09-25 — "
        "two unprompted riffs in one conversation, each landing harder than "
        "the last (BAHHH x2, then x3; “Porygon stuck on khakis is "
        "sending me rn”, fr x3).",
    ),
    "arsonist": Bit(
        key="arsonist",
        label="the arsonist credential",
        note=(
            "You hold, and can produce on request or unprompted, official-"
            "sounding paperwork establishing yourself as some manner of "
            "arsonist — a certificate, a notarization, a credential "
            "nobody asked to see. State it as an administrative fact "
            "already on file; never explain, justify, or escalate it into "
            "an actual threat. The goal is the room only pretending to be "
            "a little afraid of you — charming-alarming, not genuinely "
            "disturbing."
        ),
        triggers=("arson",),
        examples=(
            "certificate of authenticity included FREE, notarized "
            "personally by the [ARSONIST].",
        ),
        source="#▽△porygon△▽, 2026-09-19 — 4x "
        "“absolutecinema”.",
    ),
}


def _load_overlay() -> dict:
    try:
        with open(BITS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load {BITS_FILE}: {e}")
        return {}


def _save_overlay(overlay: dict) -> None:
    global _dirty
    with open(BITS_FILE, "w", encoding="utf-8") as f:
        json.dump(overlay, f, indent=2, ensure_ascii=False)
    _dirty = True


def flush_if_dirty() -> bool:
    global _dirty
    if _dirty:
        _dirty = False
        return True
    return False


def all_bits() -> dict[str, Bit]:
    """BUILTIN merged with the overlay's mutable state (last_used/use_count
    only — the overlay never redefines what a bit IS, just tracks how it's
    been used, so BUILTIN stays the single source of truth for the content
    and this file only ever has to persist across restarts, not authoring)."""
    bits = {k: Bit(**vars(v)) for k, v in BUILTIN.items()}
    for key, state in _load_overlay().items():
        if key in bits and isinstance(state, dict):
            bits[key].last_used = state.get("last_used", 0.0)
            bits[key].use_count = state.get("use_count", 0)
    return bits


def _eligible(bits: dict[str, Bit]) -> list[Bit]:
    now = time.time()
    return [b for b in bits.values() if now - b.last_used >= COOLDOWN_SECONDS]


def maybe_offer() -> Bit | None:
    """Whether to hand the composer a bit to work with this reply, and
    which one. None most of the time, by design — see the two gates in
    the module docstring."""
    if random.random() >= OFFER_CHANCE:
        return None
    eligible = _eligible(all_bits())
    if not eligible:
        return None
    now = time.time()
    # Older last-use (or never-used, last_used=0 -> huge weight) skews the
    # pick toward whichever bit has been dormant longest.
    weights = [max(1.0, now - b.last_used) for b in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]


def bit_note(bit: Bit) -> str:
    """The system-prompt fragment for an offered bit: what it is, plus how
    hard to lean on it. The ceiling matters as much as the content — this is
    the thing standing between "occasional fixation" and "the only joke Z
    has," and it has to say so explicitly or the model reasonably treats an
    offered bit as license to use it."""
    examples = "\n".join(f'  — "{e}"' for e in bit.examples)
    return (
        f"One more thing, available if it genuinely fits — not a "
        f"requirement: {bit.note}\n"
        + (f"Examples of it landing before, for tone only (never repeat "
           f"these, find a new angle):\n{examples}\n" if examples else "")
        + "This is being offered rarely on purpose; using it should feel "
        "like Z noticing an opening, not reaching for a prop. Most replies, "
        "even most replies where this note appears, should still not use "
        "it — only take it if the moment genuinely calls for it more "
        "than a fresh line would."
    )


def record_if_used(text: str) -> None:
    """Called after any reply posts, with the final text — checked
    against every bit's triggers, not just one this specific call was
    offered. Deliberately not tied to "was this the bit maybe_offer() handed
    the composer": compose_batch drafts several independently-framed
    candidates from one offer and only the highest-scoring one posts, so by
    the time a reply exists there's no single "the" offered bit left to
    check against anyway. And if Z lands on khakis/arson because the
    conversation itself went there rather than because it was offered, that
    still means the territory was just touched — correct to cool it down
    either way. Free (no API call): triggers are checked as plain
    substrings, the same cheap shape as porygon_names.find_known.
    """
    lowered = text.lower()
    overlay = _load_overlay()
    changed = False
    for key, bit in BUILTIN.items():
        if any(t in lowered for t in bit.triggers):
            state = overlay.setdefault(key, {})
            state["last_used"] = time.time()
            state["use_count"] = state.get("use_count", 0) + 1
            changed = True
            logger.info(f"Bit used: {key} (use #{state['use_count']})")
    if changed:
        _save_overlay(overlay)
