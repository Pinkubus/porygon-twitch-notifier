"""
porygon_voice.py — plain Porygon's voice: the "normal one."

Porygon Z (z_brain.py) has a whole personality: a deadpan bureaucratic
creature, glitch text, invented devices, a multi-draft comedy pipeline
judged against a humor rubric. Plain Porygon — the other bot in the
server, the one that runs the quote bot, reaction roles, feature requests
and the social auto-poster — has none of that, on purpose. When something
needs to come from *him* instead of Z (the control panel's manual-post
tool is the only caller today), it should read like an ordinary bot typed
it: short, plain, no costume.

So this mirrors z_brain's shape — same low-level call (`z_brain._call`,
same usage tracking, same "truncated thinking" safety net) and the same
"say only what's needed" discipline — but strips everything that makes a
line sound like Z: no first-person ban, no CAPS bark, no {{glitch}} marks,
no zalgo at post time, no bureaucrat bit. One draft, no critique pass —
there's no joke to punch up, so there's nothing a second pass would fix
that a clearer first pass didn't already get right.
"""
from __future__ import annotations

import os
from typing import Optional

import porygon_names
import z_brain

MODEL = os.environ.get("PORYGON_VOICE_MODEL", z_brain.MODEL_COMPOSE)
MAX_WORDS = int(os.environ.get("PORYGON_VOICE_MAX_WORDS", 60))

PERSONA = f"""\
You are Porygon, a Discord bot. Not a character, not a bit — you are what \
you sound like: a plain, ordinary server bot posting a message. You are not \
Porygon Z, a separate bot that shares this server and has its own \
personality (glitch text, a deadpan bureaucratic-creature act, invented \
devices) — that voice is his, not yours, and none of it belongs here.

Your voice:
- Normal capitalization and punctuation. Complete, plain sentences.
- Concise: say only what the message needs, {MAX_WORDS} words maximum. \
Shorter is fine and often better; don't pad a short message out to look \
more thorough.
- Plain, common English. No jargon, no invented terminology, no persona, no \
catchphrase.
- No glitch text, no zalgo, no ALL-CAPS for emphasis, no {{{{double-brace}}}} \
markup, no bureaucratic voice, no character costume of any kind.
- First person ("I'll post that", "this is now live") is normal here — you \
are not performing a bit about avoiding it.
- Friendly or matter-of-fact, whichever the message calls for. You are not \
trying to be funny and you are not deadpan for effect; say the thing \
straightforwardly.
- No hashtags, no quotation marks around the whole message, no preamble \
like "Porygon:". Output only the message text itself.
- Discord markdown (bold, bullet lists with "-", inline code with \
backticks) is fine when it makes the message clearer — a how-to list \
should actually be a list.
"""


def _father_note() -> str:
    """Same underlying name-tracking as Z's (porygon_names, subject=
    "father"), without Z's own quirk of narrating in third person even
    when replying to him directly — that's a personality trait, not a
    fact about who father is."""
    return porygon_names.address_note(subject="father")


def compose(prompt: str, context: str = "") -> Optional[str]:
    """One plain reply/post in Porygon's voice. No drafts, no critique pass,
    no humor judging — father is steering directly (this is only ever
    called from an explicit manual trigger), so there's no ambiguity about
    what to say, just how to say it.
    """
    system = PERSONA + "\n" + _father_note()
    parts = []
    if context:
        parts.append(f"Context — the message this is replying to, or the situation it's about:\n{context}")
    parts.append(f"What to say:\n{prompt}")
    user = "\n\n".join(parts)
    return z_brain._call(MODEL, system, user, max_tokens=800, purpose="porygon_voice_manual")


def for_post(text: str) -> str:
    """How a composed line leaves for Discord: unchanged. Z dresses up every
    mention of father (caps, triangles, glitch) because performing that is
    the whole bit; Porygon has no bit, so a mention of father here is just
    the word "father", exactly as it wrote it."""
    return text
