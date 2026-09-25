"""Shared contract between the coordinator and specialist agents.

Specialists never talk to MCP directly: they receive a ScopedGateway that
  * always passes the right case_id,
  * only allows the tools the agent declared in ``tools``,
  * records every evidence_ref returned by the server in a per-case ledger
    (the coordinator only lets ledger refs reach the final output),
  * emits ``tool_result_consumed`` in the trace,
  * applies a timeout and bounded retries to transient failures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

ISSUE_CODES: tuple[str, ...] = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
)
ENTITY_KEYS: tuple[str, ...] = (
    "order_ids",
    "item_ids",
    "seller_ids",
    "payment_references",
    "shipment_ids",
)
PARTY_TYPES = frozenset(
    {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
)

TraceValue = str | int | float | bool | None


@dataclass
class IssueDetail:
    """Conclusions that only make sense if ``primary_issue`` ends up being this issue."""

    # cause codes, most likely first, e.g. "SELLER_HANDOFF_LATE" (^[A-Z][A-Z0-9_]{2,79}$)
    root_causes: list[str] = field(default_factory=list)
    # {"party_type": one of PARTY_TYPES, "party_id": str | None}
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)
    # {"reason_code", "amount_brl", "entity_id"}
    refund_lines: list[dict[str, Any]] = field(default_factory=list)
    # action codes (<= 80 chars). When get_policy is available the coordinator uses
    # the policy's recommended_action instead, so prefer that vocabulary.
    actions: list[str] = field(default_factory=list)


@dataclass
class SpecialistResult:
    """What a specialist hands back to the coordinator.

    Only put in what the agent actually observed through MCP. Leave a field empty
    instead of guessing.

    The coordinator decides ``primary_issue`` from everyone's ``issue_signals`` and
    then keeps ONLY the winning issue's details, so an agent that lost the vote can
    never leak its actions, parties or refund lines into the final output.
    """

    agent: str
    # issue code -> strength in [0, 1]; the coordinator picks the strongest.
    issue_signals: dict[str, float] = field(default_factory=dict)
    # ENTITY_KEYS -> ids seen in evidence (order_ids, item_ids, ...).
    entities: dict[str, list[str]] = field(default_factory=dict)
    # evidence_refs this agent relies on (must come from ScopedGateway.call results).
    evidence_refs: list[str] = field(default_factory=list)
    # items shaped like the schema's claimAssessment:
    # {"claim_id", "verdict", "confidence", "evidence_refs"}
    claims: list[dict[str, Any]] = field(default_factory=list)
    # items shaped like the schema's dataConflict
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    # small scalar facts for the trace (never prompts or reasoning)
    notes: dict[str, TraceValue] = field(default_factory=dict)
    # Preferred: per-issue conclusions, e.g. {"duplicate_charge": IssueDetail(...)}.
    issue_details: dict[str, IssueDetail] = field(default_factory=dict)
    # Policy agent only: the ``rules`` dict from get_policy (issue -> case_status,
    # recommended_action, refund_brl, responsible_parties). Add the get_policy
    # evidence_ref to ``evidence_refs`` as usual.
    policy_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Legacy shortcut: these four flat fields are treated as the details of THIS
    # agent's strongest signalled issue. Use ``issue_details`` when an agent can
    # signal more than one issue.
    root_causes: list[str] = field(default_factory=list)
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)
    refund_lines: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def strongest_issue(self) -> str | None:
        signals = {c: s for c, s in self.issue_signals.items() if c in ISSUE_CODES and s > 0}
        if not signals:
            return None
        return min(signals, key=lambda code: (-signals[code], ISSUE_CODES.index(code)))

    def details_for(self, issue: str) -> IssueDetail:
        if issue in self.issue_details:
            return self.issue_details[issue]
        flat = IssueDetail(
            self.root_causes, self.responsible_parties, self.refund_lines, self.actions
        )
        has_flat = any(
            (flat.root_causes, flat.responsible_parties, flat.refund_lines, flat.actions)
        )
        if has_flat and self.strongest_issue() == issue:
            return flat
        return IssueDetail()


class ToolFailure(RuntimeError):
    """MCP call failed after the failure policy was applied."""

    def __init__(self, tool_name: str, kind: str, detail: str = "") -> None:
        super().__init__(f"{tool_name}: {kind}" + (f" ({detail})" if detail else ""))
        self.tool_name = tool_name
        self.kind = kind  # "timeout" | "transport" | "tool_error"


class ScopedGateway:
    TRANSIENT = (TimeoutError, ConnectionError, OSError)

    def __init__(
        self,
        gateway: EvidenceGateway,
        *,
        case_id: str,
        actor: str,
        allowed_tools: Iterable[str],
        trace: TraceWriter,
        ledger: dict[str, str],
        cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]],
        timeout: float = 30.0,
        retries: int = 2,
        backoff: float = 0.5,
    ) -> None:
        self._gateway = gateway
        self.case_id = case_id
        self.actor = actor
        self.allowed_tools = frozenset(allowed_tools)
        self._trace = trace
        self._ledger = ledger
        self._cache = cache
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff
        self._emitted: set[str] = set()

    async def call(self, tool_name: str, **arguments: str) -> dict[str, Any]:
        if tool_name not in self.allowed_tools:
            raise PermissionError(f"{self.actor} may not call {tool_name}")
        key = (tool_name, tuple(sorted(arguments.items())))
        evidence = self._cache.get(key)
        if evidence is None:
            evidence = await self._fetch(tool_name, arguments)
            self._cache[key] = evidence
        ref = evidence["evidence_ref"]
        self._ledger.setdefault(ref, tool_name)
        if ref not in self._emitted:  # one trace event per actor per evidence keeps the trace small
            self._emitted.add(ref)
            self._trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=self.actor,
                tool_name=tool_name,
                evidence_refs=[ref],
            )
        return evidence

    async def _fetch(self, tool_name: str, arguments: dict[str, str]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                return await asyncio.wait_for(
                    self._gateway.call(tool_name, case_id=self.case_id, **arguments),
                    timeout=self._timeout,
                )
            except self.TRANSIENT as exc:
                last = exc
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * (2**attempt))
            except RuntimeError as exc:  # server said the call failed: not retried
                raise ToolFailure(tool_name, "tool_error", str(exc)[:120]) from exc
        kind = "timeout" if isinstance(last, TimeoutError) else "transport"
        raise ToolFailure(tool_name, kind, type(last).__name__) from last


@dataclass
class CaseContext:
    case: dict[str, Any]
    gateway: ScopedGateway
    trace: TraceWriter
    # results of agents that already ran, keyed by agent name
    prior: dict[str, SpecialistResult] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    @property
    def claimed_order_id(self) -> str | None:
        """The order the customer *says* is affected. Not ground truth."""
        return self.case.get("customer_request", {}).get("claimed_order_id")

    @property
    def policy_version(self) -> str | None:
        return self.case.get("policy_version")


class Specialist(Protocol):
    name: str
    tools: frozenset[str]

    async def run(self, ctx: CaseContext) -> SpecialistResult: ...
