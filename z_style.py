"""
z_style.py — Z's running style note: a short, evolving description of his own
comedic sensibility, built from the replies he actually posts.

Speech training (z_speech/ in z_brain) hands Z a random transcript clip as a
comedic influence, and every reply is a fresh call, so on its own his style
would wander from line to line. This is the memory that lets it settle: after
each posted prose reply, a small Haiku call (Z_STYLE_MODEL) folds what that
line did into a handful of bullets in z_style_notes.json, and those bullets
ride along in every compose. Only posted replies feed it, and a reply is only
posted once it has cleared Z's own humor scoring, so what accumulates is the
stuff that worked.

The note describes moves and instincts, never specific lines, words or
devices, so it can steer him without turning into a list of catchphrases
(which the recent-replies rule in z_brain is fighting the whole time).
Delete z_style_notes.json to start his style over.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
from typing import Optional

import z_brain

logger = logging.getLogger("porygon.z_style")

_HERE = os.path.dirname(os.path.abspath(__file__))
STYLE_FILE = os.path.join(_HERE, "z_style_notes.json")
MODEL = os.environ.get("Z_STYLE_MODEL", "claude-haiku-4-5-20251001")
MAX_NOTES = int(os.environ.get("Z_STYLE_MAX_NOTES", 8))
# Fraction of posted replies that update the note. Each update is one small
# Haiku call; 1.0 means every reply.
UPDATE_RATE = float(os.environ.get("Z_STYLE_UPDATE_RATE", 1.0))

_lock = threading.Lock()


def load() -> list[str]:
    try:
        with open(STYLE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    return [n for n in (notes or []) if isinstance(n, str) and n.strip()][:MAX_NOTES]


def _save(notes: list[str], updates: int) -> None:
    tmp = STYLE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"notes": notes, "updates": updates}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STYLE_FILE)


def _updates() -> int:
    try:
        with open(STYLE_FILE, encoding="utf-8") as f:
            return int(json.load(f).get("updates", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def prompt_note() -> str:
    """The note as it goes into Z's system prompt, or "" before any exists."""
    notes = load()
    if not notes:
        return ""
    bullets = "\n".join(f"- {n}" for n in notes)
    return (
        "Your own comedic style so far. This is what you have been growing "
        "into, distilled from your lines that landed. Lean on it, keep "
        "building it, and feel free to surprise it. It describes how you "
        "think, not words to reuse; never repeat a line or device because "
        "it is implied here.\n" + bullets + "\n"
    )


_SYSTEM = f"""\
You maintain a short running description of a comedy bot's personal sense of \
humor: the moves and instincts that make its jokes its own. The bot is a \
deadpan, bureaucratic, slightly unwell little server pet, and it is \
developing its own style partly out of influences like confident absurd \
escalation, oddly specific swerves and brags that collapse.

You get the current notes and one new line the bot just posted (it was good \
enough to post). Update the notes so they capture the bot's style a little \
better, folding in whatever this line shows about how it thinks.

Rules:
- At most {MAX_NOTES} bullets, each one short sentence.
- Describe moves, instincts, rhythm and attitude. Never quote the line, never \
record specific words, topics, objects, names or devices; those would turn \
into catchphrases.
- Merge or sharpen existing bullets rather than piling up near-duplicates. \
Drop a bullet only if a better one replaces it. If the line shows nothing \
new, return the notes unchanged.
- Output only the bullets, one per line, each starting with "- ".
"""


def record(line: str) -> None:
    """Fold one posted reply into the style note. Never raises."""
    if not line or not line.strip() or random.random() >= UPDATE_RATE:
        return
    try:
        with _lock:
            notes = load()
            current = "\n".join(f"- {n}" for n in notes) or "(none yet)"
            raw = z_brain._call(
                MODEL, _SYSTEM,
                f"Current notes:\n{current}\n\nNew line:\n{line.strip()}",
                max_tokens=500, purpose="style_update",
            )
            if not raw:
                return
            new = [ln.strip()[2:].strip() for ln in raw.splitlines()
                   if ln.strip().startswith("- ") and len(ln.strip()) > 3]
            if not new:
                return
            _save(new[:MAX_NOTES], _updates() + 1)
    except Exception:
        logger.exception("style note update failed")
