"""
z_poll_modes.py — the purpose codes `!poll` draws on.

A poll command says how wide (`!poll_10`), how far back (`tp_all_time`) and
*what for* — a short acronym like `CDS` that carries a whole instruction about
what kind of decision this batch is meant to serve. The codes exist because
the interesting part of a poll request is almost never the wording, it's the
job: converging on a decision reads the history differently than taking the
room's temperature on something already proposed, and both read it
differently than splitting up work.

Builtins live in BUILTIN below. Anything the server owner adds with
`!pollmode` is layered on top from z_poll_modes.json, so new codes survive a
restart and can override a builtin without a code change.
"""
from __future__ import annotations

import os
import re
import json
import logging

logger = logging.getLogger("porygon.z_poll_modes")

_HERE = os.path.dirname(os.path.abspath(__file__))
MODES_FILE = os.path.join(_HERE, "z_poll_modes.json")

KEY_RE = re.compile(r"^[A-Za-z]{2,6}$")

ADD_COMMAND = "!pollmode"
LIST_COMMAND = "!pollmodes"

# key -> {label, purpose, novel?}. `purpose` is dropped into the planning
# prompt as the mission for that batch; it should say what the polls are FOR,
# not how to format them — the formatting rules are the same for every mode.
#
# `novel` flips one rule the purpose text can't reliably win on its own: with
# it set, options must be things the history does NOT contain, and the
# planner's default "include what people actually suggested, then expand"
# becomes "poll outside everything already named". Every other mode reads the
# history for material; a novel one reads it for the boundary. It's a builtin
# field — `!pollmode` doesn't set it, and an owner override of a builtin key
# keeps whatever the builtin had.
BUILTIN: dict[str, dict] = {
    "CDS": {
        "label": "collaborative decision system",
        "purpose": (
            "The group is circling a decision and needs to converge. Work out "
            "every open axis the discussion has left unresolved, expand each "
            "into a full set of reasonable choices, and put them up so the "
            "room can narrow itself down. Favour the axes where a decision "
            "would actually unblock the others."
        ),
    },
    "IDE": {
        "label": "ideation spread",
        "novel": True,
        "purpose": (
            "Put material on the table that isn't on it yet. Every option has "
            "to be something nobody in the history has said — a genre, "
            "structure, hook, setting, format or constraint the group has not "
            "named — not a finer slice of one they have, and not a re-listing "
            "of the ideas already floating. Read the history for what KIND of "
            "thing they're making and why, then go past it into what's "
            "adjacent. This is not a decision: never poll which of the "
            "existing ideas to pursue, prioritise, commit to or drop, and "
            "never offer 'stick with what we have', 'do both' or 'something "
            "else' as options — those are CDS and FIN's job, and filing one "
            "here wastes the poll. Ask what people find interesting, not what "
            "they should do. Keep it low-stakes and exploratory, prefer "
            "multi-select so people can back several things at once."
        ),
    },
    "SCP": {
        "label": "scope check",
        "purpose": (
            "Pin down how big this actually is. Poll the boundaries: what's "
            "in, what's out, how long people are willing to spend, what "
            "counts as finished, and which parts are must-have versus "
            "nice-to-have. Options should be concrete sizes and cutoffs, not "
            "vague ambitions."
        ),
    },
    "PRI": {
        "label": "priority order",
        "purpose": (
            "Things are already on the table; the question is what happens "
            "first. Poll for ordering and relative importance among what's "
            "been raised, and use multi-select where the honest question is "
            "'which of these matter at all' rather than 'which is first'."
        ),
    },
    "TMP": {
        "label": "temperature check",
        "purpose": (
            "Something specific has been proposed and you're measuring "
            "appetite for it, not generating alternatives. Poll how people "
            "actually feel — enthusiasm, reservations, dealbreakers, "
            "willingness to commit. Options should be honest positions "
            "including the lukewarm and negative ones, never a menu that only "
            "allows agreement."
        ),
    },
    "BRK": {
        "label": "tiebreak",
        "purpose": (
            "A live disagreement needs settling. Find the actual point of "
            "contention rather than the surface argument, state it neutrally "
            "in a way both sides would accept as fair, and give each real "
            "position its own option — including any middle ground nobody has "
            "voiced yet. Do not editorialise toward either side."
        ),
    },
    "SCH": {
        "label": "scheduling",
        "purpose": (
            "Work out when. Poll days, times, cadence or deadlines, using "
            "whatever the history says about people's availability and "
            "timezones. Prefer multi-select for availability — the useful "
            "answer is every slot someone can make, not their single "
            "favourite."
        ),
    },
    "NAM": {
        "label": "naming",
        "purpose": (
            "Pick a name, title or label. Include every candidate that's been "
            "floated, then expand with strong options in the same spirit that "
            "nobody has proposed. Keep each option to the bare name, without "
            "commentary or explanation attached."
        ),
    },
    "RET": {
        "label": "retrospective",
        "purpose": (
            "Something already happened; this is about learning from it. Poll "
            "what worked, what didn't, and what should change next time, "
            "drawing the options from what people actually experienced in the "
            "history. Keep it blameless — options describe decisions and "
            "outcomes, never people."
        ),
    },
    "RSK": {
        "label": "risk check",
        "purpose": (
            "Find what's most likely to sink this before it does. Poll the "
            "failure modes, blockers and unknowns — both the ones raised in "
            "the discussion and the obvious ones nobody has mentioned — so "
            "the group can see where to spend its caution. Multi-select "
            "usually fits."
        ),
    },
    "SPL": {
        "label": "split the work",
        "purpose": (
            "Divide up the doing. Poll how the work should break apart, which "
            "pieces people want to own, and what nobody wants to touch. Use "
            "what the history says about who has been doing or volunteering "
            "for what, without assigning anyone anything by name."
        ),
    },
    "FIN": {
        "label": "finalize",
        "purpose": (
            "Something has been circling long enough to be locked in. State "
            "the decision as it currently stands, as plainly and accurately "
            "as the history supports, and poll whether to ratify it, amend it "
            "or reopen it — with the specific amendments as their own "
            "options where the discussion suggests them."
        ),
    },
}


def _load_overlay() -> dict:
    try:
        with open(MODES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load {MODES_FILE}: {e}")
        return {}


def _save_overlay(overlay: dict) -> None:
    with open(MODES_FILE, "w", encoding="utf-8") as f:
        json.dump(overlay, f, indent=2, ensure_ascii=False)


def all_modes() -> dict[str, dict]:
    """Builtins with the owner's additions layered over them."""
    modes = dict(BUILTIN)
    for key, entry in _load_overlay().items():
        if isinstance(entry, dict) and entry.get("purpose"):
            # Merged rather than replaced so rewording a builtin's purpose
            # doesn't silently drop its `novel` flag along with it.
            modes[key.upper()] = {**BUILTIN.get(key.upper(), {}), **entry}
    return modes


def get(key: str) -> dict | None:
    return all_modes().get((key or "").upper())


def is_mode(token: str) -> bool:
    return bool(token) and KEY_RE.match(token) is not None and get(token) is not None


ADD_FORMAT = (
    f"`{ADD_COMMAND} KEY | short label | what the polls are for`\n"
    "• **KEY** — 2-6 letters, e.g. `RSK`. Reusing an existing key "
    "replaces it.\n"
    "• **short label** — a few words, shown in this menu.\n"
    "• **what the polls are for** — a sentence or three telling me "
    "what job this batch of polls is doing: what to look for in the history, "
    "what kind of options to build, anything to avoid. Say what it's FOR, not "
    "how to format it — the limits and formatting rules are the same for "
    "every code.\n\n"
    f"example:\n`{ADD_COMMAND} BUD | budget check | Work out what people are "
    "willing to spend, in money and in hours. Poll concrete amounts and "
    "ceilings drawn from what's been said, including the option of spending "
    "nothing.`"
)


def parse_add(rest: str) -> tuple[str, str, str]:
    """`KEY | label | purpose` -> the three fields. Raises ValueError with a
    postable explanation."""
    parts = [p.strip() for p in (rest or "").split("|")]
    if len(parts) < 3:
        raise ValueError(f"i need three parts separated by `|`.\n\n{ADD_FORMAT}")
    key, label = parts[0].upper(), parts[1]
    # Everything after the second pipe is the purpose, so it may contain pipes.
    purpose = "|".join(parts[2:]).strip()
    if not KEY_RE.match(key):
        raise ValueError(f"`{parts[0]}` isn't a usable key — 2 to 6 letters, no digits.")
    if key in (ADD_COMMAND.upper(), LIST_COMMAND.upper()):
        raise ValueError("that one's taken.")
    if not label:
        raise ValueError(f"the label is empty.\n\n{ADD_FORMAT}")
    if len(purpose) < 20:
        raise ValueError(
            "the purpose is too thin to be useful — tell me what the polls "
            f"are actually for.\n\n{ADD_FORMAT}"
        )
    return key, label, purpose


def add(key: str, label: str, purpose: str) -> bool:
    """Store a mode. True if it replaced an existing one."""
    overlay = _load_overlay()
    existed = key.upper() in overlay or key.upper() in BUILTIN
    overlay[key.upper()] = {"label": label, "purpose": purpose}
    _save_overlay(overlay)
    return existed


def menu_lines() -> list[str]:
    """One line per mode for the DM'd menu, builtins first."""
    modes, overlay = all_modes(), _load_overlay()
    order = [k for k in BUILTIN if k in modes] + sorted(
        k for k in modes if k not in BUILTIN
    )
    lines = []
    for key in order:
        entry = modes[key]
        purpose = " ".join(str(entry.get("purpose", "")).split())
        if len(purpose) > 120:
            purpose = purpose[:119].rstrip() + "…"
        marker = " *(yours)*" if key in overlay else ""
        if entry.get("novel"):
            marker += " *(new ground only — options must be things nobody's said)*"
        lines.append(f"**{key}** — {entry.get('label', '')}{marker}\n  {purpose}")
    return lines


def menu_messages(chunk_limit: int = 1900) -> list[str]:
    """The whole menu, split into Discord-sized messages."""
    header = (
        "\U0001f4cb **the `!poll` command**\n"
        "`!poll_<N> tp_<period> <CODE> [extra steer]` — e.g. "
        "`!poll_10 tp_all_time CDS`.\n"
        "**how many options:**\n"
        "• `!poll_N` — **up to** N. a ceiling: i stop when the good answers "
        "run out and i won't pad a poll with filler to reach it.\n"
        "• `!poll=N` — **exactly** N. a target: i keep reaching until i hit "
        "it, and only come up short when the question genuinely hasn't got N "
        "distinct answers.\n"
        "• 10 is discord's hard cap either way. bare `!poll` is 5.\n"
        "• the last slot is always **`Something else`**, and it counts toward "
        "N — `!poll=10` is nine ideas and a way out. if 3 or more of you pick "
        "it, i take that as the list being wrong rather than the question, "
        "and re-file that one poll with a different set. twice at most.\n"
        "**how far back:** `tp_all_time`, `tp_today`, or "
        "`tp_<n>_<minutes|hours|days|weeks>`.\n"
        "**what for:** drop a code from the list below after the period. "
        "plain words with no code still work.\n"
        "**steering:** anything you type after the code refines it rather "
        "than replacing it — `!poll=10 tp_today IDE lean cheap to build` is "
        "still an ideation batch, pointed somewhere.\n"
        "how many polls is my call, not a number you pass.\n"
    )
    footer = f"\n—\nto add your own:\n{ADD_FORMAT}\n\n`{LIST_COMMAND}` shows this again."
    messages, current = [], header
    for line in menu_lines():
        if len(current) + len(line) + 2 > chunk_limit:
            messages.append(current.rstrip())
            current = ""
        current += "\n" + line + "\n"
    if len(current) + len(footer) > chunk_limit:
        messages.append(current.rstrip())
        current = ""
    messages.append((current + footer).strip())
    return [m for m in messages if m]
