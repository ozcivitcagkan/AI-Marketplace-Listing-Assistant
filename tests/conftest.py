import pytest

from listing_assistant.config import Settings
from listing_assistant.db import open_database
from listing_assistant.db.repositories import ListingRepository
from listing_assistant.models import Listing


@pytest.fixture
def conn(tmp_path):
    """A fresh, fully migrated database file per test."""
    connection = open_database(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def listing(conn):
    return ListingRepository(conn).add(Listing())


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", max_photos_per_listing=3)


@pytest.fixture
def schema():
    from listing_assistant.field_schema import load_category_schema
    from listing_assistant.models import Category

    return load_category_schema(Category.CAR)
