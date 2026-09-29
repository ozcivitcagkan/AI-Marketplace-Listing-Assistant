"""Market Analyst: an informational price range from SYNTHETIC comparables (doc §9.1).

Simple MVP form, fully in code: SQL filter -> statistics -> widen the filter when the
sample is too small (at most 3 widening steps, each reported to the seller). No model
is involved, and comparable descriptions are never read. The result is not a price
recommendation and is never placed in the listing text.
"""

from dataclasses import dataclass

from listing_assistant.models import Fact, MarketStatus, MarketSummary
from listing_assistant.tools import AgentRun
from listing_assistant.toolset import PriceStats


@dataclass(frozen=True)
class SearchStep:
    year_spread: int
    use_city: bool
    note_tr: str | None


# First the tightest filter, then up to three documented widenings.
SEARCH_LADDER = [
    SearchStep(1, True, None),
    SearchStep(2, True, "Model yılı aralığı ±2 yıla genişletildi."),
    SearchStep(3, True, "Model yılı aralığı ±3 yıla genişletildi."),
    SearchStep(3, False, "Diğer şehirlerdeki ilanlar da dahil edildi."),
]


def run_market_analyst(run: AgentRun) -> MarketSummary:
    facts: list[Fact] = run.call("get_listing_facts")
    values = {f.field_key: f.value for f in facts}
    if not all(key in values for key in ("make", "model", "model_year")):
        return MarketSummary(status=MarketStatus.MISSING_FACTS)

    year = int(values["model_year"])
    city = values.get("city")
    notes: list[str] = []
    for step in SEARCH_LADDER:
        if not step.use_city and city is None:
            continue  # without a city this widening changes nothing
        if step.note_tr:
            notes.append(step.note_tr)
        prices: list[int] = run.call(
            "search_comparables",
            make=values["make"],
            model=values["model"],
            year_min=year - step.year_spread,
            year_max=year + step.year_spread,
            city=city if step.use_city else None,
        )
        stats: PriceStats | None = run.call("compute_price_stats", prices=prices)
        if stats is not None:
            return MarketSummary(
                status=MarketStatus.OK,
                sample_size=stats.sample_size,
                median_price_try=stats.median,
                q1_price_try=stats.q1,
                q3_price_try=stats.q3,
                widening_notes=notes,
            )
    return MarketSummary(status=MarketStatus.INSUFFICIENT_DATA, widening_notes=notes)
