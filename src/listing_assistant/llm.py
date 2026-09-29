"""The single boundary between our code and a language model.

Agents only see the `LLMClient` protocol, so tests plug in a deterministic fake and no
agent imports the Anthropic SDK. Whatever the model returns is validated against the
request's Pydantic output model; anything else raises instead of being "repaired".
"""

import base64
import threading
from dataclasses import dataclass, field
from typing import Protocol

import anthropic
import pydantic
from pydantic import BaseModel

# Server-side refusal fallback (re-runs a declined request on another model) is only
# offered for these models; see the Claude API docs on `fallbacks: "default"`.
_FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """Base class for every model failure an agent may need to handle."""


class LLMUnavailableError(LLMError):
    """Credentials, network, rate limit or server problems: retrying later may help."""


class LLMOutputError(LLMError):
    """The model answered, but not in the required shape (or it refused)."""


class BudgetExceededError(LLMError):
    """The per-listing LLM call budget is spent (architecture doc §5, cost control)."""


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    jpeg_bytes: bytes = field(repr=False)


@dataclass(frozen=True)
class LLMRequest:
    system: str
    parts: tuple[TextPart | ImagePart, ...]
    output_model: type[BaseModel]
    prompt_version: str
    max_tokens: int = 16000


@dataclass(frozen=True)
class LLMResult:
    output: BaseModel
    model: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    def generate(self, request: LLMRequest) -> LLMResult: ...


class LlmBudget:
    """Counts LLM calls for one listing; shared by all agents in one workflow step."""

    def __init__(self, limit: int, used: int) -> None:
        self.limit = limit
        self.used = used
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise BudgetExceededError(f"Bu ilan için {self.limit} model çağrısı sınırı doldu.")
            self.used += 1


class AnthropicLLMClient:
    def __init__(self, model: str, client: anthropic.Anthropic | None = None) -> None:
        self._model = model
        # Credentials come from the environment (ANTHROPIC_API_KEY or `ant auth login`).
        self._client = client if client is not None else anthropic.Anthropic()

    def generate(self, request: LLMRequest) -> LLMResult:
        kwargs = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": [_to_block(p) for p in request.parts]}],
            "output_format": request.output_model,
        }
        if self._model in _FALLBACK_MODELS:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            response = self._client.beta.messages.parse(**kwargs)
        except anthropic.AuthenticationError:
            raise LLMUnavailableError("Claude API anahtarı eksik veya geçersiz.") from None
        except anthropic.RateLimitError:
            raise LLMUnavailableError(
                "Claude API istek sınırına ulaşıldı; biraz sonra tekrar deneyin."
            ) from None
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(
                f"Claude API hatası (HTTP {exc.status_code}); biraz sonra tekrar deneyin."
            ) from None
        except anthropic.APIConnectionError:
            raise LLMUnavailableError(
                "Claude API'ye ulaşılamadı; internet bağlantınızı kontrol edin."
            ) from None
        except (pydantic.ValidationError, ValueError) as exc:
            raise LLMOutputError(
                f"Model beklenen biçimde cevap vermedi ({type(exc).__name__})."
            ) from None
        except anthropic.AnthropicError as exc:
            raise LLMUnavailableError(f"Claude istemci hatası ({type(exc).__name__}).") from None

        if response.stop_reason == "refusal":
            raise LLMOutputError("Model bu isteği yanıtlamayı reddetti.")
        if response.stop_reason == "max_tokens":
            raise LLMOutputError("Modelin cevabı yarıda kesildi.")
        if response.parsed_output is None:
            raise LLMOutputError("Model beklenen biçimde cevap vermedi.")
        try:
            # Validate again with our own strict model rather than trusting the helper.
            output = request.output_model.model_validate(response.parsed_output.model_dump())
        except pydantic.ValidationError:
            raise LLMOutputError("Model beklenen biçimde cevap vermedi.") from None
        return LLMResult(
            output=output,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


def _to_block(part: TextPart | ImagePart) -> dict:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(part.jpeg_bytes).decode("ascii"),
        },
    }
