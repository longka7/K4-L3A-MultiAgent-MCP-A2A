from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.agents.contract import (
    CaseContext,
    IssueDetail,
    ScopedGateway,
    SpecialistResult,
    ToolFailure,
)
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import assemble, decide, run_case

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


CASE_CLAIMS = {
    "case_id": "CASE_001",
    "customer_request": {
        "claims": [
            {"claim_id": "c-a", "topic": "canceled_order_paid"},
            {"claim_id": "c-b", "topic": "requested_full_refund"},
        ]
    },
}
LEDGER = {REF_A: "get_order", REF_B: "get_policy"}


def party(kind: str, party_id: str | None = None) -> dict[str, object]:
    return {"party_type": kind, "party_id": party_id}


def line(code: str, amount: float) -> dict[str, object]:
    return {"reason_code": code, "amount_brl": amount, "entity_id": "o1"}


def rule(status: str, action: str, refund: float, kind: str, party_id: str | None = None) -> dict:
    return {
        "case_status": status,
        "recommended_action": action,
        "refund_brl": refund,
        "responsible_parties": [party(kind, party_id)],
    }


def test_refund_pending_defaults_to_needs_investigation() -> None:
    result = SpecialistResult(agent="p", issue_signals={"refund_pending": 0.9})
    assert decide([result])[:2] == ("refund_pending", "needs_investigation")


def test_losing_issue_details_do_not_leak_into_output() -> None:
    payment = SpecialistResult(
        agent="payment",
        issue_signals={"duplicate_charge": 0.6},
        issue_details={
            "duplicate_charge": IssueDetail(
                root_causes=["DUPLICATE_CAPTURE"],
                responsible_parties=[party("payment_provider")],
                refund_lines=[line("DUPLICATE", 64)],
                actions=["refund_duplicate_charge"],
            )
        },
        evidence_refs=[REF_A],
    )
    shipment = SpecialistResult(
        agent="shipment",
        issue_signals={"late_delivery_seller": 0.95},
        issue_details={
            "late_delivery_seller": IssueDetail(
                root_causes=["SELLER_HANDOFF_LATE"],
                responsible_parties=[party("seller", "s1")],
                refund_lines=[line("FREIGHT", 18)],
                actions=["refund_freight"],
            )
        },
        evidence_refs=[REF_B],
    )
    output = assemble(CASE_CLAIMS, [payment, shipment], LEDGER)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["resolution_actions"] == ["refund_freight"]
    assert output["root_cause_analysis"]["responsible_parties"] == [party("seller", "s1")]
    assert output["root_cause_analysis"]["ranked_causes"] == [
        {"cause_code": "SELLER_HANDOFF_LATE", "rank": 1}
    ]
    assert output["financial_resolution"]["recommended_refund_brl"] == 18


def test_legacy_flat_fields_only_count_for_the_agents_strongest_issue() -> None:
    payment = SpecialistResult(
        agent="payment",
        issue_signals={"duplicate_charge": 0.9},
        actions=["refund_duplicate_charge"],
        refund_lines=[line("DUPLICATE", 64)],
    )
    shipment = SpecialistResult(agent="shipment", issue_signals={"late_delivery_seller": 0.99})
    output = assemble(CASE_CLAIMS, [payment, shipment], LEDGER)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["resolution_actions"] == []
    assert output["financial_resolution"]["refund_lines"] == []


def test_policy_rule_sets_status_action_and_party_type() -> None:
    payment = SpecialistResult(
        agent="payment",
        issue_signals={"payment_mismatch": 0.9},
        responsible_parties=[party("platform")],  # wrong type: the policy says payment_provider
        actions=["reconcile_it"],
        refund_lines=[line("OVERPAID", 5)],
        evidence_refs=[REF_A],
    )
    policy = SpecialistResult(
        agent="policy",
        policy_rules={
            "payment_mismatch": rule(
                "action_required", "reconcile_payment", 35.0, "payment_provider"
            )
        },
        evidence_refs=[REF_B],
    )
    output = assemble(CASE_CLAIMS, [payment, policy], LEDGER)
    assert output["resolution_actions"] == ["reconcile_payment"]
    assert output["root_cause_analysis"]["responsible_parties"] == [party("payment_provider")]
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["refund_lines"] == [
        {"reason_code": "OVERPAID", "amount_brl": 5.0, "entity_id": "o1"}
    ]


def test_zero_refund_policy_clears_refund_and_keeps_consistency() -> None:
    payment = SpecialistResult(
        agent="payment",
        issue_signals={"valid_split_payment": 0.9},
        refund_lines=[line("SPLIT", 10)],
        evidence_refs=[REF_A],
    )
    policy = SpecialistResult(
        agent="policy",
        policy_rules={
            "valid_split_payment": rule("no_action", "document_no_action", 0.0, "customer")
        },
        evidence_refs=[REF_B],
    )
    output = assemble(CASE_CLAIMS, [payment, policy], LEDGER)
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 0,
        "refund_lines": [],
    }
    assert output["resolution_actions"] == ["document_no_action"]


def test_seller_id_comes_from_evidence_not_the_policys_fixed_sample() -> None:
    seller_rule = {
        "late_delivery_seller": rule("action_required", "refund_freight", 18, "seller", "seller-x")
    }
    signals = {"late_delivery_seller": 0.9}
    policy = SpecialistResult(agent="policy", policy_rules=seller_rule, evidence_refs=[REF_B])
    with_id = SpecialistResult(
        agent="s", issue_signals=signals, responsible_parties=[party("seller", "real-seller")]
    )
    without_id = SpecialistResult(agent="s", issue_signals=signals)
    got = assemble(CASE_CLAIMS, [with_id, policy], LEDGER)
    assert got["root_cause_analysis"]["responsible_parties"] == [party("seller", "real-seller")]
    got = assemble(CASE_CLAIMS, [without_id, policy], LEDGER)
    assert got["root_cause_analysis"]["responsible_parties"] == [party("seller", None)]


def verdicts(output: dict) -> dict[str, str]:
    return {c["claim_id"]: c["verdict"] for c in output["claim_assessments"]}


def test_every_input_claim_gets_an_assessment() -> None:
    agree = SpecialistResult(
        agent="a", issue_signals={"canceled_order_paid": 0.9}, evidence_refs=[REF_A]
    )
    assert verdicts(assemble(CASE_CLAIMS, [agree], LEDGER)) == {
        "c-a": "supported",
        "c-b": "partially_supported",
    }
    no_action = SpecialistResult(agent="a", issue_signals={"unsupported_claim": 0.9})
    assert verdicts(assemble(CASE_CLAIMS, [no_action], LEDGER)) == {
        "c-a": "unsupported",
        "c-b": "unsupported",
    }
    nothing = SpecialistResult(agent="a")
    assert verdicts(assemble(CASE_CLAIMS, [nothing], LEDGER)) == {
        "c-a": "insufficient_evidence",
        "c-b": "insufficient_evidence",
    }


def test_specialist_claim_is_not_duplicated_by_the_fallback() -> None:
    claim = {"claim_id": "c-a", "verdict": "unsupported", "confidence": 0.7, "evidence_refs": []}
    agent = SpecialistResult(agent="a", issue_signals={"canceled_order_paid": 0.9}, claims=[claim])
    output = assemble(CASE_CLAIMS, [agent], LEDGER)
    assert [c["claim_id"] for c in output["claim_assessments"]] == ["c-a", "c-b"]
    assert verdicts(output)["c-a"] == "unsupported"


def test_confidence_is_capped_when_there_is_no_evidence() -> None:
    agent = SpecialistResult(agent="a", issue_signals={"duplicate_charge": 0.95})
    assert assemble(CASE_CLAIMS, [agent], {})["assessment"]["confidence"] <= 0.2


def test_policy_decided_is_traced_only_when_a_policy_rule_applies(tmp_path: Path) -> None:
    async def policy(ctx: CaseContext) -> SpecialistResult:
        evidence = await ctx.gateway.call("get_policy", policy_version="EC_POLICY_V1")
        return SpecialistResult(
            agent="policy-agent",
            issue_signals={"refund_failed": 0.9},
            evidence_refs=[evidence["evidence_ref"]],
            policy_rules={
                "refund_failed": rule("action_required", "retry_refund", 52, "payment_provider")
            },
        )

    trace, path = make_trace(tmp_path)
    output = asyncio.run(
        run_case(
            CASE,
            FakeGateway(),  # type: ignore[arg-type]
            trace,
            [Agent("policy-agent", {"get_policy"}, policy)],
            lambda output, ctx: [],
        )
    )
    decided = [e for e in events(path) if e["event_type"] == "policy_decided"]
    assert len(decided) == 1
    assert decided[0]["decision_code"] == "refund_failed"
    assert decided[0]["attributes"] == {"case_status": "action_required", "action": "retry_refund"}
    assert output["resolution_actions"] == ["retry_refund"]

    trace2, path2 = make_trace(tmp_path / "second")
    asyncio.run(
        run_case(
            CASE,
            FakeGateway(),  # type: ignore[arg-type]
            trace2,
            [Agent("x-agent", set(), lambda ctx: _empty(ctx))],
            lambda output, ctx: [],
        )
    )
    assert not [e for e in events(path2) if e["event_type"] == "policy_decided"]


async def _empty(ctx: CaseContext) -> SpecialistResult:
    return SpecialistResult(agent="x-agent")


def test_trace_has_one_consumed_event_per_actor_and_evidence(tmp_path: Path) -> None:
    trace, path = make_trace(tmp_path)
    fake = FakeGateway()

    def gateway(actor: str) -> ScopedGateway:
        return ScopedGateway(
            fake,  # type: ignore[arg-type]
            case_id="CASE_001",
            actor=actor,
            allowed_tools={"get_order"},
            trace=trace,
            ledger={},
            cache={},
        )

    async def go() -> None:
        first = gateway("order-agent")
        await first.call("get_order", order_id="o1")
        await first.call("get_order", order_id="o1")
        await gateway("payment-agent").call("get_order", order_id="o1")

    asyncio.run(go())
    consumed = [(e["actor"], e["evidence_refs"]) for e in events(path)]
    assert consumed == [("order-agent", [REF_A]), ("payment-agent", [REF_A])]
