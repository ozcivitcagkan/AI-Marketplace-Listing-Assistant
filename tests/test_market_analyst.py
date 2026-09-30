import sqlite3

import pytest

from fakes import FakeLLMClient
from listing_assistant.agents.market_analyst import run_market_analyst
from listing_assistant.db.repositories import ComparableRepository, FactRepository
from listing_assistant.llm import LlmBudget
from listing_assistant.models import (
    AgentName,
    Fact,
    FactSource,
    FactStatus,
    MarketStatus,
)
from listing_assistant.synthetic_market import (
    Comparable,
    generate_comparables,
    seed_if_empty,
)
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import ListingTools, price_stats


def approve(conn, listing, **values):
    repo = FactRepository(conn)
    for key, value in values.items():
        repo.add(
            Fact(
                listing_id=listing.id,
                field_key=key,
                value=value,
                source=FactSource.USER,
                status=FactStatus.APPROVED,
            )
        )


def analyse(conn, settings, schema, listing):
    tools = ListingTools(conn, settings, schema, listing.id).as_mapping()
    llm = FakeLLMClient(lambda r: AssertionError("the market analyst never calls a model"))
    run = AgentRun(AgentName.MARKET_ANALYST, listing.id, tools, llm, LlmBudget(40, 0))
    return run_market_analyst(run), run


def comparable(year, city="İzmir", price=500_000):
    return Comparable("Renault", "Clio", year, 80_000, city, price, "Sentetik ilan")


def test_generator_is_deterministic_and_synthetic():
    first, second = generate_comparables(), generate_comparables()
    assert first == second
    assert len(first) == 480
    assert all(row.description.startswith("Sentetik ilan") for row in first)


def test_seeding_happens_once(conn):
    assert seed_if_empty(conn) == 480
    assert seed_if_empty(conn) == 0


def test_non_synthetic_rows_are_refused_by_the_database(conn):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), conn:
        conn.execute(
            "INSERT INTO comparable_listings (make, model, model_year, mileage_km, city,"
            " price_try, description, is_synthetic) VALUES ('A','B',2020,1,'X',1,'real',0)"
        )


def test_statistics_are_computed_by_code():
    assert price_stats([100, 200, 300, 400]) is None  # below the minimum sample of 5
    stats = price_stats([100, 200, 300, 400, 500])
    assert (stats.sample_size, stats.q1, stats.median, stats.q3) == (5, 200, 300, 400)


def test_exact_match_needs_no_widening(conn, settings, schema, listing):
    ComparableRepository(conn).add_many([comparable(2019, price=p) for p in range(1, 6)])
    approve(conn, listing, make="renault", model="CLIO", model_year="2019", city="izmir")
    summary, _ = analyse(conn, settings, schema, listing)
    assert summary.status is MarketStatus.OK
    assert summary.sample_size == 5 and summary.widening_notes == []
    assert summary.is_synthetic


@pytest.mark.parametrize("typed", ["Istanbul", "istanbul", "ISTANBUL", "İSTANBUL"])
def test_city_filter_folds_dotted_and_dotless_i(conn, settings, schema, listing, typed):
    rows = [comparable(2019, city="İstanbul", price=p) for p in range(1, 6)]
    ComparableRepository(conn).add_many(rows)
    approve(conn, listing, make="Renault", model="Clio", model_year="2019", city=typed)
    summary, _ = analyse(conn, settings, schema, listing)
    assert summary.status is MarketStatus.OK
    assert summary.widening_notes == []


def test_filter_widens_step_by_step_and_says_so(conn, settings, schema, listing):
    rows = [comparable(2016, city="Ankara", price=p) for p in range(100, 105)]
    ComparableRepository(conn).add_many(rows)
    approve(conn, listing, make="Renault", model="Clio", model_year="2019", city="İzmir")
    summary, run = analyse(conn, settings, schema, listing)

    assert summary.status is MarketStatus.OK
    assert len(summary.widening_notes) == 3
    assert "şehir" in summary.widening_notes[-1]
    assert [c.tool_name for c in run.tool_calls].count("search_comparables") == 4


def test_insufficient_data_gives_no_range(conn, settings, schema, listing):
    ComparableRepository(conn).add_many([comparable(2019)])
    approve(conn, listing, make="Renault", model="Clio", model_year="2019")
    summary, _ = analyse(conn, settings, schema, listing)
    assert summary.status is MarketStatus.INSUFFICIENT_DATA
    assert summary.median_price_try is None


def test_missing_facts_are_reported(conn, settings, schema, listing):
    approve(conn, listing, make="Renault")
    summary, _ = analyse(conn, settings, schema, listing)
    assert summary.status is MarketStatus.MISSING_FACTS
