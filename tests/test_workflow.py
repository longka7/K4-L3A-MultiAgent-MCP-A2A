from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.agents.contract import (
    CaseContext,
    ScopedGateway,
    SpecialistResult,
    ToolFailure,
)
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import decide, run_case

ROOT = Path(__file__).resolve().parents[1]
REF_A = "ev_" + "a" * 24
REF_B = "ev_" + "b" * 24
FAKE_REF = "ev_" + "z" * 24
CASE = {"case_id": "CASE_001", "policy_version": "EC_POLICY_V1"}


class FakeGateway:
    def __init__(self, behaviour: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.behaviour = behaviour

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if self.behaviour:
            return await self.behaviour(len(self.calls))
        ref = REF_A if tool_name == "get_order" else REF_B
        return {"evidence_ref": ref, "domain": "order", "data": {}}


class Agent:
    def __init__(self, name: str, tools: set[str], fn: Any) -> None:
        self.name, self.tools, self._fn = name, frozenset(tools), fn

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        return await self._fn(ctx)


def make_trace(tmp_path: Path) -> tuple[TraceWriter, Path]:
    path = tmp_path / "trace.jsonl"
    return TraceWriter(path, Contracts(ROOT / "contracts" / "schemas")), path


def events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_decide_without_signals_is_insufficient_evidence() -> None:
    issue, status, confidence = decide([SpecialistResult(agent="a")])
    assert (issue, status) == ("insufficient_evidence", "needs_investigation")
    assert 0 <= confidence <= 1


def test_decide_is_deterministic_on_ties_and_maps_status() -> None:
    a = SpecialistResult(agent="a", issue_signals={"duplicate_charge": 0.8})
    b = SpecialistResult(agent="b", issue_signals={"late_delivery_seller": 0.8})
    assert decide([a, b])[0] == "late_delivery_seller"  # earlier in ISSUE_CODES wins the tie
    assert decide([a, b]) == decide([b, a])
    no_action = SpecialistResult(agent="c", issue_signals={"valid_split_payment": 0.9})
    assert decide([no_action])[:2] == ("valid_split_payment", "no_action")


def test_run_case_emits_lifecycle_and_drops_unseen_evidence(tmp_path: Path) -> None:
    trace, path = make_trace(tmp_path)

    async def order(ctx: CaseContext) -> SpecialistResult:
        evidence = await ctx.gateway.call("get_order", order_id="o1")
        return SpecialistResult(
            agent="order",
            issue_signals={"canceled_order_paid": 0.9},
            entities={"order_ids": ["o1"]},
            evidence_refs=[evidence["evidence_ref"], FAKE_REF],  # FAKE_REF never came from MCP
            claims=[
                {
                    "claim_id": "c1",
                    "verdict": "supported",
                    "confidence": 0.9,
                    "evidence_refs": [REF_A, FAKE_REF],
                }
            ],
            root_causes=["ORDER_CANCELED_AFTER_PAYMENT", "bad code"],
            responsible_parties=[{"party_type": "seller", "party_id": "s1"}],
            refund_lines=[{"reason_code": "FULL", "amount_brl": 10.005, "entity_id": "o1"}],
            actions=["refund_customer"],
        )

    output = asyncio.run(
        run_case(
            CASE,
            FakeGateway(),  # type: ignore[arg-type]
            trace,
            [Agent("order-agent", {"get_order"}, order)],
            lambda output, ctx: [],
        )
    )

    trace.contracts.validate_output(output, "output")
    assert output["evidence_refs"] == [REF_A]
    assert output["claim_assessments"][0]["evidence_refs"] == [REF_A]
    assert output["root_cause_analysis"]["ranked_causes"] == [
        {"cause_code": "ORDER_CANCELED_AFTER_PAYMENT", "rank": 1}
    ]
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == pytest.approx(10.0, abs=0.01)

    kinds = [e["event_type"] for e in events(path)]
    for required in ("task_assigned", "tool_result_consumed", "handoff", "verification_completed"):
        assert required in kinds
    assert kinds.index("task_assigned") < kinds.index("verification_completed")
    consumed = next(e for e in events(path) if e["event_type"] == "tool_result_consumed")
    assert consumed["evidence_refs"] == [REF_A] and consumed["actor"] == "order-agent"


def test_failing_specialist_does_not_abort_case(tmp_path: Path) -> None:
    trace, path = make_trace(tmp_path)

    async def boom(ctx: CaseContext) -> SpecialistResult:
        raise ValueError("bad payload")

    output = asyncio.run(
        run_case(
            CASE,
            FakeGateway(),  # type: ignore[arg-type]
            trace,
            [Agent("x-agent", set(), boom)],
            lambda output, ctx: [],
        )
    )
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert any(e.get("decision_code") == "specialist_failed" for e in events(path))
    trace.contracts.validate_output(output, "output")


def test_malformed_specialist_data_falls_back_to_schema_valid_output(tmp_path: Path) -> None:
    trace, path = make_trace(tmp_path)

    async def sloppy(ctx: CaseContext) -> SpecialistResult:
        return SpecialistResult(
            agent="s",
            issue_signals={"duplicate_charge": 0.9},
            claims=[{"claim_id": "c", "verdict": "maybe", "confidence": 5, "evidence_refs": []}],
        )

    output = asyncio.run(
        run_case(
            CASE,
            FakeGateway(),  # type: ignore[arg-type]
            trace,
            [Agent("s-agent", set(), sloppy)],
            lambda output, ctx: [],
        )
    )
    trace.contracts.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert (
        "schema_fallback"
        in next(e for e in events(path) if e["event_type"] == "verification_completed")[
            "attributes"
        ]["issues"]
    )


def scoped(gateway: FakeGateway, tmp_path: Path, tools: set[str], **kw: Any) -> ScopedGateway:
    trace, _ = make_trace(tmp_path)
    return ScopedGateway(
        gateway,  # type: ignore[arg-type]
        case_id="CASE_001",
        actor="t-agent",
        allowed_tools=tools,
        trace=trace,
        ledger={},
        cache={},
        backoff=0,
        **kw,
    )


def test_scoped_gateway_enforces_tool_ownership_case_id_and_cache(tmp_path: Path) -> None:
    fake = FakeGateway()
    gw = scoped(fake, tmp_path, {"get_order"})
    with pytest.raises(PermissionError):
        asyncio.run(gw.call("get_policy", policy_version="v1"))

    async def twice() -> None:
        await gw.call("get_order", order_id="o1")
        await gw.call("get_order", order_id="o1")

    asyncio.run(twice())
    assert fake.calls == [("get_order", {"case_id": "CASE_001", "order_id": "o1"})]


def test_scoped_gateway_retries_timeouts_then_fails(tmp_path: Path) -> None:
    async def always_timeout(_: int) -> dict[str, Any]:
        raise TimeoutError

    fake = FakeGateway(always_timeout)
    gw = scoped(fake, tmp_path, {"get_order"}, retries=2)
    with pytest.raises(ToolFailure) as info:
        asyncio.run(gw.call("get_order", order_id="o1"))
    assert info.value.kind == "timeout" and len(fake.calls) == 3


def test_scoped_gateway_does_not_retry_tool_errors(tmp_path: Path) -> None:
    async def not_found(_: int) -> dict[str, Any]:
        raise RuntimeError("MCP tool get_order failed: not found")

    fake = FakeGateway(not_found)
    gw = scoped(fake, tmp_path, {"get_order"})
    with pytest.raises(ToolFailure) as info:
        asyncio.run(gw.call("get_order", order_id="o1"))
    assert info.value.kind == "tool_error" and len(fake.calls) == 1
