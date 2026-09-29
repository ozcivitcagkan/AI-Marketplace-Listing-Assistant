"""Optional live check against the real Claude API. Skipped unless explicitly enabled:

    $env:RUN_LIVE_TESTS = "1"; pytest tests/test_live_api.py

It costs a few API calls. It only checks that real responses fit our contracts; the
synthetic image contains no car, so the model should report little or nothing.
"""

import os

import pytest

from listing_assistant.agent_io import PhotoAnalysis
from listing_assistant.config import load_settings
from listing_assistant.llm import AnthropicLLMClient, ImagePart, LLMRequest, TextPart
from listing_assistant.prompting import load_prompt
from synthetic_images import encode, synthetic_scene

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to call the real API"
)


def test_real_vision_call_returns_a_valid_photo_analysis():
    prompt = load_prompt("vision_analyst_v1")
    client = AnthropicLLMClient(load_settings().model)
    result = client.generate(
        LLMRequest(
            system=prompt.text,
            parts=(
                ImagePart(encode(synthetic_scene())),
                TextPart("Allowed fields:\n- color (text)"),
            ),
            output_model=PhotoAnalysis,
            prompt_version=prompt.version,
        )
    )
    assert isinstance(result.output, PhotoAnalysis)
    assert result.input_tokens > 0
