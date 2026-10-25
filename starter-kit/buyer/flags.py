"""Switches for A/B tests: the same code with one component off, chosen by environment variables.

BUYER_OFF=probe,match      components to turn off (names below); default: everything on
BUYER_CLAIMS=1             at most this many new arguments per message (default 3, the arena checks 3)
BUYER_LEAN=1               small fixed steps that keep a gap, single-supplier cases only (`Planner.target_value`)
BUYER_CLAUSE0=1            a required index clause: also offer its deadband-0 variant at equal cost (`Planner.sheets`)
BUYER_DEEP=1               raw material opens 18 % below, without the committee cap (`Planner._anchor_gap`)
All three lost live (EXPERIMENTS.md) and are off by default; they stay for further A/B tests.

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


def lean():
    return os.environ.get("BUYER_LEAN") == "1"


def clause0():
    return os.environ.get("BUYER_CLAUSE0") == "1"


def deep():
    return os.environ.get("BUYER_DEEP") == "1"
