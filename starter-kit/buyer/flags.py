"""Switches for A/B tests: the same code with one component off, chosen by environment variables.

BUYER_OFF=probe,match      components to turn off (names below); default: everything on
BUYER_CLAIMS=1             at most this many new arguments per message (default 3, the arena checks 3)
BUYER_PACE=1               spread verified arguments: 2 in the first message with a sheet, then 1 fresh one (weight
                           >= 0.2) per message; none in a message without a sheet
BUYER_FLOOR=1              chase a supplier that still steps to its floor: no early accept or settle while it moves in
                           steady steps; quality cases offer only the tightest ppm (`Planner.chasing`, `refresh`)
BUYER_FINALC=1             after its final offer take the sheet (standing or alternative) with the lowest C for us
                           All but the last lost live (EXPERIMENTS.md) and are off by default; they stay for further A/B tests.
BUYER_LIKE=1               read the supplier's first move on like terms for the floor/limit estimates (`first_move`)
BUYER_QSTEP=1              quality cases: steps >= 0.55 % of its opening while it is >= 2 % above ours
                           (both W3b, off: see EXPERIMENTS.md 3.2)
BUYER_NIBBLE=1             before accepting a single supplier's open offer, one counter a small step below it (`nibble`)
BUYER_STRAT=0            on by default, 0 turns it off: one approach per supplier thread (Leverage, Floor-chase, Quick-close; `buyer/approach.py`),
                           chosen at case start: it sets the opening depth, sheets per message, stop rule, tactics and
                           which of PACE / FLOOR / FINALC apply to that thread (the variables above are then ignored)

profiles   the prebuilt supplier profiles (`profiles/`); off: the live read for every supplier
persona    the live persona read at case start (Opus); off: regex traits only for suppliers without a profile
probe      two offers just under the walk-away when every package looks dearer than it
match      offering its own value level on our package near the end
keepalive  holding notes when a supplier waits 45 s
menu       extra packages for the strategist to choose from; off: only the code's equal-cost sheets
plan       the negotiation plan text in the strategist's brief
playbook   the profile's playbook text in the strategist's brief
"""

import os

NAMES = {"profiles", "persona", "probe", "match", "keepalive", "menu", "plan", "playbook"}


def off():
    return {x.strip() for x in os.environ.get("BUYER_OFF", "").split(",") if x.strip()}


def on(name):
    """Whether a component is in play (read each time, so a test can set the variable)."""
    assert name in NAMES, name
    return name not in off()


def claims_per_message():
    try:
        return max(0, min(3, int(os.environ.get("BUYER_CLAIMS", "3"))))
    except ValueError:
        return 3



def pace():
    return os.environ.get("BUYER_PACE") == "1"


def floor():
    return os.environ.get("BUYER_FLOOR") == "1"


def finalc():
    return os.environ.get("BUYER_FINALC") == "1"


def strat():
    return os.environ.get("BUYER_STRAT", "1") != "0"


def like():
    """BUYER_LIKE=1: read the supplier's first move on like terms (`Planner.first_move`). Off by default: on P11's
    0.794 replies it raised the floor estimate and accepted 45.50 a round before the supplier came down to 45.00."""
    return os.environ.get("BUYER_LIKE") == "1"


def nibble():
    return os.environ.get("BUYER_NIBBLE") == "1"


def qstep():
    """BUYER_QSTEP=1: quality cases step at least 0.55 % of its opening while its price is >= 2 % above ours
    (`Planner.target_value`). Off by default: the gate's persona twin loses P11 0.05-0.06 (quality 0.80 -> 0.77 on
    seed 3); only the live back-office rule speaks for it - decide with a live A/B on P11."""
    return os.environ.get("BUYER_QSTEP") == "1"
