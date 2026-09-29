from pathlib import Path

import pytest

from listing_assistant.config import Settings, load_settings


def test_defaults_when_env_is_empty():
    settings = load_settings({})
    assert settings == Settings()
    assert settings.db_path == Path("data") / "listing_assistant.db"
    assert settings.photos_dir == Path("data") / "photos"


def test_env_overrides_defaults():
    settings = load_settings(
        {"LA_MODEL": "claude-sonnet-5", "LA_DATA_DIR": "tmp_data", "LA_MAX_PHOTOS_PER_LISTING": "5"}
    )
    assert settings.model == "claude-sonnet-5"
    assert settings.data_dir == Path("tmp_data")
    assert settings.max_photos_per_listing == 5


@pytest.mark.parametrize("value", ["abc", "0", "-3", "1.5"])
def test_invalid_limits_fail_fast(value):
    with pytest.raises(ValueError, match="LA_MAX_PHOTO_BYTES"):
        load_settings({"LA_MAX_PHOTO_BYTES": value})


def test_empty_model_is_rejected():
    with pytest.raises(ValueError, match="LA_MODEL"):
        load_settings({"LA_MODEL": "  "})


def test_api_key_is_not_stored_in_settings():
    settings = load_settings({"ANTHROPIC_API_KEY": "fake-key-for-test"})
    assert "fake-key-for-test" not in repr(settings)
