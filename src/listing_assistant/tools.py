"""Tool registry, per-agent permissions and the gate every tool call passes through.

The permission matrix is data (architecture doc §5). An agent receives an `AgentRun`,
and the only way to touch listing data is `run.call("<tool>")`: unknown tools, disabled
tools and tools outside the agent's row are refused and recorded. The tool functions
are bound to one listing by the orchestrator, so no tool takes a `listing_id` argument.
"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from listing_assistant.llm import LlmBudget, LLMClient, LLMRequest
from listing_assistant.models import (
    AgentName,
    AgentRunRecord,
    RunStatus,
    ToolCallRecord,
    new_id,
    utc_now,
)

A = AgentName


class ToolKind(StrEnum):
    READ = "read"
    READ_LLM = "read_llm"
    DRAFT_WRITE = "draft_write"
    FLOW = "flow"
    EXTERNAL = "external"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    risk: Risk
    allowed_agents: frozenset[AgentName]
    enabled: bool = True


def _spec(name, kind, risk, *agents, enabled=True) -> tuple[str, ToolSpec]:
    return name, ToolSpec(name, kind, risk, frozenset(agents), enabled)


# Architecture doc §5. Nothing here approves, exports, deletes, changes status or reads
# another listing: those actions exist only as human-triggered workflow methods.
TOOL_REGISTRY: Mapping[str, ToolSpec] = dict(
    [
        _spec(
            "get_listing_facts",
            ToolKind.READ,
            Risk.LOW,
            A.FACT_RECONCILER,
            A.GAP_DETECTOR,
            A.MARKET_ANALYST,
            A.COPYWRITER,
            A.SAFETY_REVIEWER,
        ),
        _spec(
            "get_listing_photos",
            ToolKind.READ,
            Risk.LOW,
            A.INTAKE_GUARD,
            A.PHOTO_CURATOR,
            A.VISION_ANALYST,
        ),
        _spec("vision_describe", ToolKind.READ_LLM, Risk.MEDIUM, A.PHOTO_CURATOR, A.VISION_ANALYST),
        _spec("find_duplicates", ToolKind.READ, Risk.LOW, A.PHOTO_CURATOR),
        _spec("pii_scan_text", ToolKind.READ, Risk.LOW, A.INTAKE_GUARD, A.SAFETY_REVIEWER),
        _spec("search_comparables", ToolKind.READ, Risk.LOW, A.MARKET_ANALYST),
        _spec("compute_price_stats", ToolKind.READ, Risk.LOW, A.MARKET_ANALYST),
        _spec("get_style_rules", ToolKind.READ, Risk.LOW, A.COPYWRITER, A.SAFETY_REVIEWER),
        _spec(
            "save_fact_proposals",
            ToolKind.DRAFT_WRITE,
            Risk.MEDIUM,
            A.VISION_ANALYST,
            A.FACT_RECONCILER,
        ),
        _spec("save_draft", ToolKind.DRAFT_WRITE, Risk.MEDIUM, A.COPYWRITER),
        _spec("request_user_input", ToolKind.FLOW, Risk.LOW, A.GAP_DETECTOR),
        # Registered so the decision is explicit and testable: disabled for every agent.
        _spec("web_search", ToolKind.EXTERNAL, Risk.HIGH, enabled=False),
    ]
)


class PermissionDeniedError(PermissionError):
    pass


def summarize_args(kwargs: Mapping[str, Any]) -> str:
    """Describe arguments by type and size only, so no personal data reaches the logs."""
    parts = []
    for key in sorted(kwargs):
        value = kwargs[key]
        if isinstance(value, str | bytes | list | tuple | dict | set):
            parts.append(f"{key}={type(value).__name__}(len={len(value)})")
        else:
            parts.append(f"{key}={type(value).__name__}")
    return ", ".join(parts)[:500]


class AgentRun:
    """One execution of one agent: its permission-checked toolbox, LLM access and trace."""

    def __init__(
        self,
        agent: AgentName,
        listing_id: str,
        tools: Mapping[str, Callable[..., Any]],
        llm: LLMClient,
        budget: LlmBudget,
    ) -> None:
        self.id = new_id()
        self.agent = agent
        self.listing_id = listing_id
        self.started_at = utc_now()
        self.tool_calls: list[ToolCallRecord] = []
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.model: str | None = None
        self.prompt_version: str | None = None
        self.failure: str | None = None
        self._tools = tools
        self._llm = llm
        self._budget = budget
        # Vision calls run in threads; the trace and counters must stay consistent.
        self._lock = threading.Lock()

    def is_allowed(self, tool_name: str) -> bool:
        spec = TOOL_REGISTRY.get(tool_name)
        return (
            spec is not None
            and spec.enabled
            and self.agent in spec.allowed_agents
            and tool_name in self._tools
        )

    def call(self, tool_name: str, **kwargs: Any) -> Any:
        summary = summarize_args(kwargs)
        if not self.is_allowed(tool_name):
            self._record(tool_name, allowed=False, summary=summary, error="PermissionDeniedError")
            raise PermissionDeniedError(f"{self.agent} may not call {tool_name}")
        try:
            result = self._tools[tool_name](self, **kwargs)
        except Exception as exc:
            self._record(tool_name, allowed=True, summary=summary, error=type(exc).__name__)
            raise
        self._record(tool_name, allowed=True, summary=summary, error=None)
        return result

    def generate(self, request: LLMRequest):
        """Call the model through the per-listing budget and record usage."""
        self._budget.consume()
        result = self._llm.generate(request)
        with self._lock:
            self.llm_calls += 1
            self.input_tokens += result.input_tokens
            self.output_tokens += result.output_tokens
            self.model = result.model
            self.prompt_version = request.prompt_version
        return result.output

    def mark_failed(self, reason: str) -> None:
        self.failure = reason[:200]

    def to_record(self, error: str | None = None) -> AgentRunRecord:
        failure = error or self.failure
        return AgentRunRecord(
            id=self.id,
            listing_id=self.listing_id,
            agent_name=self.agent,
            status=RunStatus.FAILED if failure else RunStatus.SUCCEEDED,
            model=self.model,
            prompt_version=self.prompt_version,
            llm_calls=self.llm_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            error=failure,
            started_at=self.started_at,
            finished_at=utc_now(),
        )

    def _record(self, tool_name: str, *, allowed: bool, summary: str, error: str | None) -> None:
        with self._lock:
            self.tool_calls.append(
                ToolCallRecord(
                    tool_name=tool_name[:64], allowed=allowed, args_summary=summary, error=error
                )
            )
