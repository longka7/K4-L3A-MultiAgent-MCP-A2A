from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.agents.contract import CaseContext, ScopedGateway, SpecialistResult
from student_agent.agents.policy_verifier import PolicyAgent, verify
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
REF_POLICY = "ev_policy_" + "4" * 20


class MockGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if tool_name in self.responses:
            return self.responses[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name[:10]}_{'a' * 20}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "policy",
            "data": {},
        }


def make_context(
    tmp_path: Path,
    case_data: dict[str, Any],
    mock_responses: dict[str, dict[str, Any]],
    prior: dict[str, SpecialistResult] | None = None,
) -> CaseContext:
    trace_path = tmp_path / "trace.jsonl"
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(trace_path, contracts)
    gateway = MockGateway(mock_responses)
    scoped = ScopedGateway(
        gateway,  # type: ignore[arg-type]
        case_id=case_data["case_id"],
        actor="policy-agent",
        allowed_tools=PolicyAgent.tools,
        trace=trace,
        ledger={},
        cache={},
    )
    return CaseContext(case=case_data, gateway=scoped, trace=trace, prior=prior or {})


def test_policy_agent_loads_rules(tmp_path: Path) -> None:
    case = {
        "case_id": "CASE_POLICY_01",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claims": [{"claim_id": "c1", "topic": "canceled_order_paid"}],
        },
    }
    policy_resp = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": REF_POLICY,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "policy",
        "data": {
            "rules": {
                "canceled_order_paid": {
                    "case_status": "action_required",
                    "recommended_action": "issue_refund",
                    "refund_brl": 100.0,
                    "responsible_parties": [{"party_type": "seller", "party_id": "s1"}],
                }
            }
        },
    }
    prior = {
        "order-agent": SpecialistResult(
            agent="order-agent",
            issue_signals={"canceled_order_paid": 0.9},
            evidence_refs=["ev_order_123"],
        )
    }

    ctx = make_context(tmp_path, case, {"get_policy": policy_resp}, prior=prior)
    agent = PolicyAgent()
    result = asyncio.run(agent.run(ctx))

    assert REF_POLICY in result.evidence_refs
    assert "canceled_order_paid" in result.policy_rules
    assert result.notes["rules_loaded"] == 1
    assert len(result.claims) == 1
    assert result.claims[0]["verdict"] == "supported"


def test_policy_agent_unsupported_claim_when_no_prior_issues(tmp_path: Path) -> None:
    case = {
        "case_id": "CASE_POLICY_02",
        "customer_request": {
            "claims": [{"claim_id": "c1", "topic": "late_delivery_seller"}],
        },
    }
    prior = {
        "order-agent": SpecialistResult(agent="order-agent", evidence_refs=["ev_order_123"]),
        "shipment-agent": SpecialistResult(agent="shipment-agent", evidence_refs=["ev_ship_123"]),
    }
    ctx = make_context(tmp_path, case, {}, prior=prior)
    agent = PolicyAgent()
    result = asyncio.run(agent.run(ctx))

    assert "unsupported_claim" in result.issue_signals
    assert result.issue_signals["unsupported_claim"] >= 0.8
    assert result.claims[0]["verdict"] == "unsupported"


def test_verify_invariants_financial_and_status(tmp_path: Path) -> None:
    case = {"case_id": "CASE_VERIFY_01"}
    ctx = make_context(tmp_path, case, {})

    # Mismatched refund sum
    output = {
        "case_id": "CASE_VERIFY_01",
        "assessment": {"primary_issue": "canceled_order_paid", "case_status": "action_required"},
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 10.0,
            "refund_lines": [{"reason_code": "CANCELED", "amount_brl": 15.0}],
        },
        "resolution_actions": ["investigate"],
        "root_cause_analysis": {"responsible_parties": []},
        "evidence_refs": ["ev_1"],
    }
    verify(output, ctx)
    assert output["financial_resolution"]["recommended_refund_brl"] == 15.0
    assert "issue_refund" in output["resolution_actions"]

    # no_action with refund
    output_no_action = {
        "case_id": "CASE_VERIFY_02",
        "assessment": {"primary_issue": "valid_split_payment", "case_status": "no_action"},
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 20.0,
            "refund_lines": [{"reason_code": "SPLIT", "amount_brl": 20.0}],
        },
        "resolution_actions": ["issue_refund"],
        "root_cause_analysis": {"responsible_parties": []},
        "evidence_refs": ["ev_1"],
    }
    verify(output_no_action, ctx)
    assert output_no_action["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output_no_action["financial_resolution"]["refund_lines"] == []
    assert not any("refund" in a for a in output_no_action["resolution_actions"])
