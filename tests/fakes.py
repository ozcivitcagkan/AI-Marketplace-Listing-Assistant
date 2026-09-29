"""A deterministic stand-in for the language model. Tests never call the real API."""

import threading
from collections.abc import Callable

import pydantic
from pydantic import BaseModel

from listing_assistant.llm import ImagePart, LLMOutputError, LLMRequest, LLMResult, TextPart

Responder = Callable[[LLMRequest], BaseModel | dict | Exception]


class FakeLLMClient:
    """Answers each request with `responder(request)` and records every request.

    Like the real client, output that does not validate against the requested model
    raises LLMOutputError, so tests can simulate malicious or malformed model output.
    """

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.requests: list[LLMRequest] = []
        self._lock = threading.Lock()

    def generate(self, request: LLMRequest) -> LLMResult:
        with self._lock:
            self.requests.append(request)
        answer = self._responder(request)
        if isinstance(answer, Exception):
            raise answer
        data = answer.model_dump() if isinstance(answer, BaseModel) else answer
        try:
            output = request.output_model.model_validate(data)
        except pydantic.ValidationError:
            raise LLMOutputError("model output failed validation") from None
        return LLMResult(output=output, model="fake-model", input_tokens=100, output_tokens=20)

    def requests_for(self, output_model: type[BaseModel]) -> list[LLMRequest]:
        return [r for r in self.requests if r.output_model is output_model]


def by_output_model(**handlers: Callable[[LLMRequest], BaseModel | dict | Exception]) -> Responder:
    """Route requests by output model class name, e.g. by_output_model(PhotoAnalysis=...)."""

    def respond(request: LLMRequest):
        handler = handlers.get(request.output_model.__name__)
        if handler is None:
            raise AssertionError(f"unexpected LLM request for {request.output_model.__name__}")
        return handler(request)

    return respond


def request_text(request: LLMRequest) -> str:
    return "\n".join(p.text for p in request.parts if isinstance(p, TextPart))


def has_image(request: LLMRequest) -> bool:
    return any(isinstance(p, ImagePart) for p in request.parts)
