"""Deterministic generator for the synthetic comparable-listing dataset (doc §9.1).

Real make/model names are used, but every price, mileage, city assignment and text is
drawn from invented distributions below. Nothing here reflects real market prices.
"""

import random
from dataclasses import dataclass

SEED = 2026
# A fixed reference year keeps the dataset identical on every machine and every year.
REFERENCE_YEAR = 2026
LISTINGS_PER_MODEL = 60

# (make, model, invented price of a new car in TRY)
MODELS = [
    ("Renault", "Clio", 1_100_000),
    ("Fiat", "Egea", 1_050_000),
    ("Toyota", "Corolla", 1_600_000),
    ("Volkswagen", "Golf", 1_750_000),
    ("Hyundai", "i20", 1_000_000),
    ("Ford", "Focus", 1_350_000),
    ("Honda", "Civic", 1_700_000),
    ("Dacia", "Duster", 1_250_000),
]
CITIES = ["İstanbul", "Ankara", "İzmir", "Bursa", "Antalya"]


@dataclass(frozen=True)
class Comparable:
    make: str
    model: str
    model_year: int
    mileage_km: int
    city: str
    price_try: int
    description: str


def generate_comparables(seed: int = SEED) -> list[Comparable]:
    # Not for security: a seeded PRNG is exactly what makes the dataset reproducible.
    rng = random.Random(seed)  # noqa: S311
    rows = []
    for make, model, new_price in MODELS:
        for _ in range(LISTINGS_PER_MODEL):
            year = rng.randint(REFERENCE_YEAR - 12, REFERENCE_YEAR)
            age = REFERENCE_YEAR - year
            mileage = max(0, round(rng.gauss(age * 16_000, 9_000), -3))
            price = (
                new_price
                * (0.88**age)
                * (1 - min(mileage, 300_000) / 1_000_000)
                * rng.uniform(0.9, 1.1)
            )
            city = rng.choice(CITIES)
            rows.append(
                Comparable(
                    make=make,
                    model=model,
                    model_year=year,
                    mileage_km=int(mileage),
                    city=city,
                    price_try=int(round(price, -4)),
                    description=f"Sentetik ilan: {year} {make} {model}, {int(mileage)} km, {city}.",
                )
            )
    return rows


def seed_if_empty(conn) -> int:
    """Load the synthetic dataset once; returns the number of rows inserted."""
    from listing_assistant.db.repositories import ComparableRepository

    repo = ComparableRepository(conn)
    if repo.count():
        return 0
    rows = generate_comparables()
    repo.add_many(rows)
    return len(rows)
