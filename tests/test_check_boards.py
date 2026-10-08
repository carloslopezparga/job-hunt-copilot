from __future__ import annotations

import datetime as dt

import yaml

from jobhunt.models import Company
from jobhunt.sources import FixtureFetcher, load_companies
from jobhunt.sources.check import BoardCheck, check_board, promote, qualifies

NORTHWIND = Company("Northwind Quant", "greenhouse", "northwindquant", "quant_trading")
PAYLANE = Company("Paylane", "lever", "paylane", "fintech", region="eu")
HELIX = Company("Helix AI", "ashby", "helixai", "ai_tech")
EMPTY = Company("Empty Co", "ashby", "emptyco", "ai_tech")
GHOST = Company("Ghost Corp", "greenhouse", "ghostcorp", "consulting")


def test_check_board_statuses(fixtures_dir):
    fetcher = FixtureFetcher(fixtures_dir)
    paylane = check_board(PAYLANE, fetcher)
    assert (paylane.status, paylane.total, paylane.spain, paylane.eu) == ("ok", 3, 1, 3)
    assert paylane.interns == 2
    northwind = check_board(NORTHWIND, fetcher)
    assert northwind.status == "ok" and northwind.spain == 0 and northwind.eu == 1  # Amsterdam
    helix = check_board(HELIX, fetcher)
    assert helix.eu >= 1  # Zurich: Switzerland is open to EU citizens
    assert check_board(EMPTY, fetcher).status == "empty"
    ghost = check_board(GHOST, fetcher)
    assert ghost.status == "not_found" and "no fixture" in ghost.error


def test_qualifies():
    ok = BoardCheck(PAYLANE, "ok", total=3, eu=0)
    assert not qualifies(ok, 1) and qualifies(ok, 0)
    assert not qualifies(BoardCheck(EMPTY, "empty"), 0)


def test_promote_appends_valid_yaml_and_skips_known(tmp_path, root):
    target = tmp_path / "companies.yaml"
    target.write_text((root / "companies.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    before = len(load_companies(target))
    already = Company("Stripe", "greenhouse", "stripe", "fintech")
    dotted = Company("Dotted Co", "ashby", "dotted.co", "ai_tech")
    checks = [
        BoardCheck(PAYLANE, "ok", total=3, spain=1, eu=3),
        BoardCheck(already, "ok", total=9, eu=2),
        BoardCheck(dotted, "ok", total=200, eu=150),
    ]
    added = promote(checks, target, dt.date(2026, 10, 8))
    assert [c.name for c in added] == ["Paylane", "Dotted Co"]
    companies = load_companies(target)  # still valid YAML, schema-checked
    assert len(companies) == before + 2
    paylane = next(c for c in companies if c.slug == "paylane")
    assert paylane.region == "eu" and paylane.sector == "fintech"
    assert "verified with `jobhunt check-boards` on 2026-10-08" in target.read_text()
    assert promote(checks, target, dt.date(2026, 10, 9)) == []  # idempotent


def test_candidates_file_is_valid_and_disjoint(root):
    candidates = load_companies(root / "companies.candidates.yaml")
    current = {(c.ats, c.slug.lower()) for c in load_companies(root / "companies.yaml")}
    assert not {(c.ats, c.slug.lower()) for c in candidates} & current
    raw = yaml.safe_load((root / "companies.candidates.yaml").read_text(encoding="utf-8"))
    for entry in raw["companies"]:  # every candidate cites where its slug came from
        assert "https://" in entry["notes"], entry["name"]


def test_removed_boards_are_gone(root):
    slugs = {(c.ats, c.slug) for c in load_companies(root / "companies.yaml")}
    for dead in [("greenhouse", "deepmind"), ("lever", "plaid"),
                 ("greenhouse", "belvederetrading"), ("lever", "mistral")]:  # fmt: skip
        assert dead not in slugs
