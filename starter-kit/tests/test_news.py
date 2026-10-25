"""T-087: every news item reaches the suppliers it concerns (market news, owner and structure kinds, kits)."""

from pathlib import Path

import pytest

from arena_kit.sim import load_cases
from buyer import claims
from buyer.dossier import MARKET_MAX, NEWS_SITUATION, NEWS_TRAITS, Dossier, World, market_situation

CASES = load_cases(Path("cases/real"))


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def row(headline, body=""):
    return {"headline": headline, "body": body}


def test_market_rows_hold_the_unassigned_items(world):
    ids = {r["id"] for r in world.market_rows}
    assert "news:20261021-S17" in ids  # the Sunday release's pump tenders item
    assert len(ids) >= 8
    assert world.memos and world.memos[0][0] == "doc:20261023-HPW-MEMO"


def test_pump_tenders_are_slack_for_pump_part_suppliers_only():
    r = row("Water utilities push pump tenders into 2027", "pump makers expect a slow winter")
    assert market_situation(r, "castings cast_iron", pump=True) == "market_slack"
    assert market_situation(r, "castings cast_iron", pump=False) is None


def test_cost_news_follows_category_and_direction():
    copper = row("Copper notation at EUR 1,289 per 100 kg", "+48 % on a year ago")
    assert market_situation(copper, "electrical cable", False) == "market_cost_up"
    assert market_situation(copper, "castings cast_iron", False) is None
    power = row("Industrial power prices -43 % on 2022")
    assert market_situation(power, "seals", False) == "market_cost_down"
    assert market_situation(row("Labour costs in manufacturing +2.7 %"), "seals", False) is None  # below the move limit


def test_market_news_lands_in_the_dossier_below_its_own(world):
    case = CASES["case:P01"]
    d = Dossier(world, case, case["suppliers"][0])
    assert len(d.market_news) <= MARKET_MAX
    assert d.current_news == d.own_news + d.market_news
    for _i, _d, topic, _h in d.market_news:
        assert NEWS_SITUATION[topic] in ("slack", "cost_up")


def test_owner_and_structure_kinds_are_read():
    for kind, situation, trait in (
        ("new_owner", "slack", "free_capacity"),
        ("succession", "approval", "approval_ladder"),
        ("approvals", "approval", "approval_ladder"),
    ):
        assert NEWS_SITUATION[kind] == situation and NEWS_TRAITS[kind] == trait
    assert "renamed" not in NEWS_SITUATION  # an alias only
    assert "new_owner" in claims.WEAK_NEWS and "market_slack" in claims.WEAK_NEWS


def test_kit_market_news_reads_every_part(world):
    for key, case in CASES.items():
        if len(case["parts"]) > 1:
            d = Dossier(world, case, case["suppliers"][0])
            assert isinstance(d.market_news, list)
