"""BUYER_STRAT: one approach per supplier thread, chosen at case start from that supplier's own profile, the case and
the rival offers on the same part, and corrected at most once from its replies.

Leverage     a supplier with room and reasons to give way: a deep opening, one fresh fact per mail, rivals held back
             until it holds, its final sheet taken by C (BUYER_PACE + BUYER_FINALC).
Floor-chase  a supplier that steps: a moderate opening, keep countering while it steps (BUYER_FLOOR + BUYER_FINALC).
Quick-close  little room or a walk risk: today's opening, one MESO pair, close in 2-3 rounds (BUYER_FINALC).

Only rules, no case numbers: every input is the profile (`profiles/sup_<id>.json`), the dossier and the case file, so a
new case with the same suppliers gets an approach the same way.
"""

from dataclasses import dataclass, field, replace

from arena_kit.cost import RELATIONSHIP_GOALS, total_cost

from .dossier import NEWS_SITUATION

LEVERAGE, FLOOR_CHASE, QUICK_CLOSE = "leverage", "floor_chase", "quick_close"
LABELS = {LEVERAGE: "Leverage", FLOOR_CHASE: "Floor-chase", QUICK_CLOSE: "Quick-close"}
# Floor-chase paces its facts too (one fresh verified fact per later mail): live P08 cited none in four mails and closed
# at 5.54 where the best team session, a fact a round, got 5.24 (gaps.md: P18/P08/P11 top sessions 1.1-1.5 per mail)
SWITCHES = {LEVERAGE: {"pace", "finalc"}, FLOOR_CHASE: {"floor", "finalc", "pace"}, QUICK_CLOSE: {"finalc"}}
SHEETS = {LEVERAGE: 2, FLOOR_CHASE: 3, QUICK_CLOSE: 2}  # MESO sheets per message
# code-side tactics each approach may use (beyond its 3 named ones): the clause trial is the MESO pair of a copper part
# (P02's fix, a quick close), so it stays in Leverage and Quick-close; the deadband-0 sheet only in Leverage
GATES = {
    LEVERAGE: {"clause_trial", "clause0", "main_source"},
    FLOOR_CHASE: {"probe", "match"},
    QUICK_CLOSE: {"clause_trial"},
}
DO_NOT = {
    LEVERAGE: "pressure words, 12-month sheets, closing on a buyer-chosen sheet before its final offer",
    FLOOR_CHASE: "accepting above its floor while it still steps, the loosest ppm sheet",
    QUICK_CLOSE: "a deep opening, more than 2-3 rounds, chasing",
}
WEAKENING = {"slack", "distress"}  # idle capacity, short-time work, restructuring, credit trouble: it needs the deal
STRENGTHENING = {"capacity", "refocus"}  # a full order book, allocation, refocus on bigger customers: it needs us less
DAY_VALUE = 0.08 / 360  # what a day of payment is worth to us: 8 % a year (0.0222 % of the price per day)
MAX_TACTICS = 3


@dataclass
class Approach:
    name: str
    opening_gap: float  # supplier-value distance below its ask for our first offer
    tactics: list  # at most 3, fixed at the start
    switches: set
    reason: str
    lead: str = ""
    trade_term: str | None = None  # the non-price term worth most to us in this case
    score: int = 0
    leverage_ok: bool = True  # never Leverage for a trusted, capacity-tight or walk-risk supplier
    corrected: str | None = None  # why it was corrected (once)
    dropped: list = field(default_factory=list)  # tactics that got no movement in 2 rounds

    @property
    def label(self):
        return LABELS[self.name]

    @property
    def sheets_n(self):
        return SHEETS[self.name]

    def allows(self, gate):
        return gate in GATES[self.name]

    def active(self, tactic):
        return tactic in self.tactics and tactic not in self.dropped


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def history(profile, dossier):
    """(n, asked %, settled %) of its past negotiations with us: the profile's events, else the dossier's."""
    neg = (profile or {}).get("negotiations") or {}
    pairs = [
        (_num(e.get("asked_pct")), _num(e.get("settled_pct")))
        for e in neg.get("events") or []
        if _num(e.get("asked_pct")) is not None and _num(e.get("settled_pct")) is not None
    ]
    if pairs:
        n = len(pairs)
        return n, sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    return getattr(dossier, "deals", None)


def news_kinds(profile, dossier):
    """{situation: headline} of its current news (the dossier's, else the profile's)."""
    out = {}
    items = [(t, h) for _i, _d, t, h in getattr(dossier, "current_news", None) or []]
    items += [(x.get("topic"), x.get("headline")) for x in (profile or {}).get("news") or []]
    for topic, headline in items:
        if topic in NEWS_SITUATION:
            out.setdefault(NEWS_SITUATION[topic], headline or topic)
    return out


def trade_term(case, dossier):
    """The negotiable non-price term that lowers our C most at today's price (payment days, ppm, contract, ...): what
    we trade for when price will not move."""
    std = dict(getattr(dossier, "standard_guess", None) or {})
    price = getattr(dossier, "reference_price", None)
    if not price:
        return None
    base_terms = {"unit_price": price}
    try:
        base = total_cost(case, base_terms, std)
    except Exception:  # noqa: BLE001 - a case the cost model cannot price without more terms
        return None
    best, gain = None, 0.0
    for f in case.get("negotiable") or []:
        if f == "unit_price":
            continue
        for opt in case.get("term_options", {}).get(f) or []:
            try:
                c = total_cost(case, {**base_terms, f: opt}, std)
            except Exception:  # noqa: BLE001
                continue
            if base - c > gain:
                best, gain = f, base - c
    return best if gain > 0.002 * base else None


def choose(profile, case, rivals=None, dossier=None, base_gap=0.08, quote=None):
    """The approach for one supplier thread. `rivals`: {supplier id: unit price} of the other bidders' offers on the
    same part; `quote`: this supplier's own quoted price (else the dossier's reference); `base_gap`: today's opening
    depth for the case (`Planner._base_gap`), the Quick-close opening."""
    profile, d = profile or {}, dossier
    has = (lambda t, strong=False: d.has(t, strong)) if d is not None else (lambda t, strong=False: False)  # noqa: E731
    score, why = 0, []

    def add(points, text):
        nonlocal score
        score += points
        why.append(f"{text} ({points:+d})")

    # a. its older behaviour with us
    if (h := history(profile, d)) and h[0] >= 2:
        n, asked, settled = h
        gives = asked - settled
        if gives >= 2.5:
            add(3, f"in {n} past negotiations it asked {asked:+.1f} % and settled {settled:+.1f} %")
        elif gives >= 2:
            add(2, f"in {n} past negotiations it asked {asked:+.1f} % and settled {settled:+.1f} %")
        elif gives >= 1:
            add(1, f"it gave {gives:.1f} points below its ask in {n} past negotiations")
        elif gives <= 0.5:
            add(-2, f"it kept its ask in {n} past negotiations ({asked:+.1f} %, settled {settled:+.1f} %)")
    pay = profile.get("payments") or {}
    if (_num(pay.get("share_paid_over_5_days_late_pct")) or 0) >= 40:
        add(-1, "we paid many of its invoices late")
    cards = (profile.get("quality") or {}).get("scorecards") or []
    if cards and ((_num(cards[-1].get("ppm")) or 0) >= 1000 or cards[-1].get("colour") == "red"):
        add(1, f"its quality record is weak ({cards[-1].get('ppm')} ppm)")
    weight = profile.get("our_weight") or {}
    share, trend = _num(weight.get("share_of_its_turnover_pct")), _num(weight.get("spend_trend_pct"))
    if share is None:  # a small firm files only a turnover band: the middle of our range is the best figure
        lo_hi = [_num(x) for x in (weight.get("share_of_its_turnover_range_pct") or [])]
        lo_hi = [x for x in lo_hi if x is not None]
        share = sum(lo_hi) / len(lo_hi) if lo_hi else None
    if share is not None and share >= 5:
        add(1, f"we are {share:.1f} % of its turnover")
    elif share is not None and share < 1:
        add(-1, f"we are only {share:.1f} % of its turnover")
    if trend is not None and trend >= 15:
        add(1, f"our spend with it grew {trend:+.0f} %")
    comp = profile.get("company") or {}
    credit, equity = _num(comp.get("credit_index")), _num(comp.get("equity_ratio_pct"))
    relationship = (case.get("buyer") or {}).get("category_goal") in RELATIONSHIP_GOALS
    if (credit is not None and credit >= 300) or (equity is not None and equity < 15):
        add(0 if relationship else 1, "its finances are weak")  # a strategic supplier is protected by the charge
    # news: what weakens it gives us room, what strengthens it takes room away
    news = news_kinds(profile, d)
    own_items = getattr(d, "own_news", None)
    if own_items is None:
        own_items = getattr(d, "current_news", None) or []
    own = {NEWS_SITUATION[t] for _i, _d, t, _h in own_items if t in NEWS_SITUATION}
    own |= {NEWS_SITUATION[x.get("topic")] for x in (profile or {}).get("news") or [] if x.get("topic") in NEWS_SITUATION}
    weak = [news[k] for k in WEAKENING if k in news and k in own]  # market news counts for less than its own
    strong = [news[k] for k in STRENGTHENING if k in news]
    for k in WEAKENING & news.keys():
        add(1 if (k == "distress" and relationship) or k not in own else 2, f"news: {news[k]}")
    for k in STRENGTHENING & news.keys():
        add(-2, f"news: {news[k]}")
    # b. what it shows now (its traits from its documents and the read)
    playbook = (profile.get("persona") or {}).get("playbook") or {}
    walk_risk = playbook.get("walk_risk") == "high"
    if has("capacity_tight"):
        add(-3, "it allocates capacity")
    if has("approval_ladder"):
        add(-1, "a pricing committee decides its larger moves")
    if walk_risk:
        add(-2, "the profile flags a high walk risk")
    if has("free_capacity") or has("stale_costing"):
        add(1, "spare capacity or an old cost sheet")
    arena = profile.get("arena") or {}
    first, sessions = _num(arena.get("first_counter_vs_opening")), _num(arena.get("sessions")) or 0
    if sessions >= 3 and first is not None:
        if first <= 0.92:
            add(1, f"its first counters give {1 - first:.0%} of its opening")
        elif first >= 0.97:
            add(-1, f"its first counters give only {1 - first:.0%} of its opening")
    # c. pricing room: its quote against our cost estimate, the rivals and our alternative
    quote = quote or (getattr(d, "letter", None) or {}).get("asked_price") or getattr(d, "reference_price", None)
    if d is not None and quote:
        full, _err = d.full_cost
        floor = full / (1 - d.min_margin) if full else None
        if floor:
            room = quote / floor - 1
            if room >= 0.15:
                add(2, f"its quote is {room:.0%} above our cost estimate")
            elif room >= 0.08:
                add(1, f"its quote is {room:.0%} above our cost estimate")
            elif room <= 0.02:
                add(-2, "its quote is at our cost estimate")
        letter = getattr(d, "letter", None) or {}
        if letter.get("asked_pct") is not None and letter["asked_pct"] - d.justified_pct >= 2:
            add(1, f"its ask beats its cost drivers by {letter['asked_pct'] - d.justified_pct:.1f} points")
    rival = min((p for p in (rivals or {}).values() if p), default=None)
    if rival and quote and len(case.get("parts") or [1]) <= 1 and rival < quote * 0.97:
        add(2, f"a rival offers {1 - rival / quote:.0%} less")
    alt = ((case.get("buyer") or {}).get("alternative") or {}).get("terms", {}).get("unit_price")
    if alt and quote and alt < quote * 0.98:
        add(1, "our alternative source is cheaper")
    # d. the case: what drives our C
    ctype, goal = case.get("type"), (case.get("buyer") or {}).get("category_goal")
    if ctype == "rfq" or len(case.get("suppliers") or []) > 1:
        add(2, "a tender with rival bidders")
    elif ctype == "raw_material":
        add(2, "a raw-material distributor quoting off its index: wide room below its quote")
    elif ctype in ("bundling", "quality"):
        add(1, f"a {ctype} case: room below its quote")
    if has("wants_clause") or (ctype == "raw_material" and "index_clause" in (case.get("negotiable") or [])):
        add(1, "it wants an index clause: the clause buys price")
    if ctype in ("price_increase", "renewal") and goal in ("stabilise", "routine", "secure_supply"):
        add(-2, f"a {goal} {ctype.replace('_', ' ')}")
    if ctype == "capacity":
        add(-2, "a capacity case")
    if relationship:
        add(-1, "a strategic category: the relationship charge makes a squeeze dear")

    trusted = bool((getattr(d, "relationship", None) or (False, []))[0])
    # a price letter or renewal has 0.3-5 % room (arena): never a deep opening there
    leverage_ok = not (
        has("capacity_tight") or walk_risk or (trusted and not rival) or ctype in ("price_increase", "renewal")
    )
    quality = "ppm_commitment" in (case.get("negotiable") or []) and (
        ctype == "quality" or bool(case.get("term_options", {}).get("ppm_commitment"))
    )
    if score >= 4 and leverage_ok and not quality:
        name = LEVERAGE
    elif score >= 1 or (quality and score >= -1):
        name = FLOOR_CHASE  # a quality case's supplier steps in fixed amounts (P11): chase, do not squeeze
    else:
        name = QUICK_CLOSE
    nudge = (0.01 if weak else 0.0) - (0.01 if strong else 0.0)  # news counts for more leverage
    tender = ctype == "rfq" or len(case.get("suppliers") or []) > 1
    if name == LEVERAGE:
        gap = min(0.19, 0.16 + 0.01 * min(3, score - 4) + nudge)
    elif name == FLOOR_CHASE:
        # 7-9 % by the signals, but never shallower than the case type's opening: on the twin 8-9 % instead of its
        # 15 % lost P12 0.67 -> 0.45 and P16 0.96 -> 0.44 (the final offer lands at its limit either way)
        own = min(0.09, 0.07 + 0.01 * max(0, score - 1))
        gap = min(0.16, max(own, min(base_gap, 0.15)) + nudge)
    else:
        # a tender keeps today's opening (twin P05: 0.07 instead of its 0.15 lost the capacity-tight bidder 0.67 -> 0.47)
        gap = max(0.03, base_gap) if tender else min(0.08, max(0.03, base_gap))
        gap = min(0.16, gap) + nudge
    if tender and name != QUICK_CLOSE:
        # every approach in a tender opens at least as deep as the case's opening: Leverage's 18 % to the 36.00 bidder
        # (29.37) drew 32.00 and a final 30.50 in live P05, the base 23 % (27.58) drew 30.50 and an agreed 27.66
        gap = max(gap, min(0.25, base_gap))
    term = trade_term(case, d) if d is not None else None
    a = Approach(
        name=name,
        opening_gap=round(gap, 4),
        tactics=tactics_for(name, case, term),
        switches=set(SWITCHES[name]),
        reason="; ".join(why) or "no strong signal in its record, news or the case",
        lead=lead_for(name, case, d, rival, term),
        trade_term=term,
        score=score,
        leverage_ok=leverage_ok,
    )
    return a


def tactics_for(name, case, term=None):
    neg = case.get("negotiable") or []
    if name == LEVERAGE:
        out = ["meso", "paced_facts", "volume_rival_trade"]
    elif name == FLOOR_CHASE:
        quality = "ppm_commitment" in neg
        out = ["meso", "tight_ppm" if quality else "steady_counters", "payment_for_price" if "payment_days" in neg else "term_trade"]
    else:
        out = ["meso_pair"]
    return out[:MAX_TACTICS]


def lead_for(name, case, d, rival, term):
    clause = "index_clause" in (case.get("negotiable") or [])
    if name == LEVERAGE:
        head = "our binding volume over the longest term" + (" with the index clause" if clause else "")
        return head + ("; the rival price once it starts holding" if rival else "; one fresh verified fact per mail")
    if name == FLOOR_CHASE:
        return "the tightest ppm sheet and patience" if "ppm_commitment" in (case.get("negotiable") or []) else (
            "steady counters while it steps" + (f"; trade {term.replace('_', ' ')} for price" if term else "")
        )
    return ("the clause package" if clause else "a volume and long-term package") + " at an honest opening"


def correct(approach, asks, case=None):
    """Once, after its second reply to us (opening + 2 replies): moved under 3 % in total -> Quick-close; a first drop of
    7 % or more -> Leverage. A new Approach, or None."""
    if approach is None or approach.corrected or len(asks) < 3 or not asks[0]:
        return None
    v0 = asks[0]
    total, first = (v0 - asks[-1]) / v0, (v0 - asks[1]) / v0
    if total < 0.03 and approach.name != QUICK_CLOSE:
        name, why = QUICK_CLOSE, f"it moved only {total:.1%} in its first two replies"
    elif first >= 0.07 and approach.name != LEVERAGE and approach.leverage_ok:
        name, why = LEVERAGE, f"its first drop was {first:.1%}"
    else:
        return None
    return replace(
        approach,
        name=name,
        tactics=tactics_for(name, case or {}),
        switches=set(SWITCHES[name]),
        corrected=why,
        dropped=[],
    )


def drop_stale(approach, asks, protected=("meso", "meso_pair")):
    """A tactic that got no movement in 2 rounds is dropped, not swapped: the last active one that is not the MESO base.
    True if one was dropped."""
    if approach is None or len(asks) < 3:
        return False
    v0 = asks[0] or 1
    if any(abs(asks[i - 1] - asks[i]) >= 0.0015 * v0 for i in (-1, -2)):
        return False
    live = [t for t in approach.tactics if t not in protected and t not in approach.dropped]
    if not live:
        return False
    approach.dropped.append(live[-1])
    return True


def payment_trade(sheet, case, std_days=None):
    """A faster-payment sheet for a supplier that asks for it: the shortest payment option below the sheet's, at a price
    cut of more than 0.0222 % a day (what a day of money is worth to us). None when nothing is faster."""
    if "payment_days" not in (case.get("negotiable") or []) or not sheet.get("unit_price"):
        return None
    days = sheet.get("payment_days") or std_days
    opts = [o for o in case.get("term_options", {}).get("payment_days") or [] if days and o < days]
    if not opts:
        return None
    new = max(opts)  # one step faster: the cheapest trade for it to grant
    cut = DAY_VALUE * (days - new) * 1.25  # a quarter more than the money is worth to us
    return {**sheet, "payment_days": new, "unit_price": round(sheet["unit_price"] * (1 - cut) - 0.01, 2)}


def granted(ask, sheet, case):
    """Our sheet already gives what it asks for (the fastest payment option, the most binding volume, the longest
    contract): then the answer is that this is priced in, not another sheet."""
    field_name = {"payment": "payment_days", "volume": "volume_per_year", "contract": "contract_months"}.get(ask)
    opts = case.get("term_options", {}).get(field_name) or []
    if field_name not in sheet or sheet.get(field_name) is None or not opts:
        return False
    best = min(opts) if ask == "payment" else max(opts)
    return sheet[field_name] <= best if ask == "payment" else sheet[field_name] >= best


def free_trade(sheet, case, field_name, need=None):
    """Binding volume up to our need, or the longest contract: free to us, so the same price. Months beyond the
    horizon neither cost nor earn (C counts years = min(M/12, H)), while the supplier values the security."""
    if field_name not in (case.get("negotiable") or []):
        return None
    opts = case.get("term_options", {}).get(field_name) or []
    if field_name == "volume_per_year" and need:
        opts = [o for o in opts if o <= need]
    if not opts or sheet.get(field_name) == max(opts):
        return None
    return {**sheet, field_name: max(opts)}
