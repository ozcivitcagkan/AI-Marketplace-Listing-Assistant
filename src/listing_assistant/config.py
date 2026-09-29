"""Application settings read from environment variables.

The Anthropic API key is deliberately NOT part of Settings: the Anthropic SDK reads
ANTHROPIC_API_KEY from the environment itself, so the secret never passes through
our code, our logs, or our objects.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "LA_"

# Generated listing text is Turkish; everything internal stays English.
LISTING_LANGUAGE = "tr"


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5"
    data_dir: Path = Path("data")
    # Limits against cost and abuse (architecture doc §7.8: "300 photos / 50 MB" attack).
    max_photos_per_listing: int = 20
    max_photo_bytes: int = 10 * 1024 * 1024
    # 20 photos (one vision call each) + notes + questions + up to 3 write/review rounds.
    max_llm_calls_per_listing: int = 40

    @property
    def db_path(self) -> Path:
        return self.data_dir / "listing_assistant.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"


def _read_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(ENV_PREFIX + name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{ENV_PREFIX}{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{ENV_PREFIX}{name} must be positive, got {value}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env` (defaults to os.environ); invalid values fail fast."""
    env = os.environ if env is None else env
    defaults = Settings()
    model = env.get(ENV_PREFIX + "MODEL", defaults.model).strip()
    if not model:
        raise ValueError(f"{ENV_PREFIX}MODEL must not be empty")
    return Settings(
        model=model,
        data_dir=Path(env.get(ENV_PREFIX + "DATA_DIR", str(defaults.data_dir))),
        max_photos_per_listing=_read_positive_int(
            env, "MAX_PHOTOS_PER_LISTING", defaults.max_photos_per_listing
        ),
        max_photo_bytes=_read_positive_int(env, "MAX_PHOTO_BYTES", defaults.max_photo_bytes),
        max_llm_calls_per_listing=_read_positive_int(
            env, "MAX_LLM_CALLS_PER_LISTING", defaults.max_llm_calls_per_listing
        ),
    )
