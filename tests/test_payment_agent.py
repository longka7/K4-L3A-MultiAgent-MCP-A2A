from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.agents.contract import CaseContext, ScopedGateway, SpecialistResult
from student_agent.agents.payment_refund import PaymentRefundAgent
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]


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
            "domain": "payment",
            "data": {},
        }


def make_context(
    tmp_path: Path,
    case_data: dict[str, Any],
    mock_responses: dict[str, dict[str, Any]],
) -> CaseContext:
    trace_path = tmp_path / "trace.jsonl"
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(trace_path, contracts)
    gateway = MockGateway(mock_responses)
    scoped = ScopedGateway(
        gateway,  # type: ignore[arg-type]
        case_id=case_data["case_id"],
        actor="payment-refund-agent",
        allowed_tools=PaymentRefundAgent.tools,
        trace=trace,
        ledger={},
        cache={},
    )
    return CaseContext(case=case_data, gateway=scoped, trace=trace)


def test_payment_agent_duplicate_charge(tmp_path: Path) -> None:
    case = {
        "case_id": "L3A_CASE_001",
        "customer_request": {
            "claimed_order_id": "ord_123",
            "claims": [{"claim_id": "c1", "topic": "duplicate_charge"}],
        },
    }
    payments_resp = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_pay_ref_" + "1" * 20,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "payment",
        "data": {
            "payments": [
                {
                    "payment_sequential": 1,
                    "payment_type": "credit_card",
                    "payment_installments": 1,
                    "payment_value": 150.0,
                    "payment_reference": "pay_ref_001",
                },
                {
                    "payment_sequential": 2,
                    "payment_type": "credit_card",
                    "payment_installments": 1,
                    "payment_value": 150.0,
                    "payment_reference": "pay_ref_002",
                },
            ],
            "total_paid": 300.0,
            "order_value": 150.0,
        },
    }

    ctx = make_context(tmp_path, case, {"get_order_payments": payments_resp})
    agent = PaymentRefundAgent()
    result = asyncio.run(agent.run(ctx))

    assert "duplicate_charge" in result.issue_signals
    assert result.issue_signals["duplicate_charge"] > 0.5
    assert "pay_ref_001" in result.entities.get("payment_references", [])
    assert "pay_ref_002" in result.entities.get("payment_references", [])
    assert any(line["amount_brl"] == 150.0 for line in result.refund_lines)


def test_payment_agent_valid_split_payment(tmp_path: Path) -> None:
    case = {
        "case_id": "L3A_CASE_005",
        "customer_request": {
            "claimed_order_id": "ord_split",
            "claims": [{"claim_id": "c2", "topic": "valid_split_payment"}],
        },
    }
    payments_resp = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_pay_ref_" + "2" * 20,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "payment",
        "data": {
            "payments": [
                {
                    "payment_sequential": 1,
                    "payment_type": "voucher",
                    "payment_installments": 1,
                    "payment_value": 50.0,
                    "payment_reference": "pay_voucher_01",
                },
                {
                    "payment_sequential": 2,
                    "payment_type": "credit_card",
                    "payment_installments": 1,
                    "payment_value": 100.0,
                    "payment_reference": "pay_card_01",
                },
            ],
            "order_value": 150.0,
        },
    }

    ctx = make_context(tmp_path, case, {"get_order_payments": payments_resp})
    agent = PaymentRefundAgent()
    result = asyncio.run(agent.run(ctx))

    assert "valid_split_payment" in result.issue_signals
    assert result.issue_signals["valid_split_payment"] > 0.5
    assert len(result.refund_lines) == 0


def test_payment_agent_refund_failed(tmp_path: Path) -> None:
    case = {
        "case_id": "L3A_CASE_009",
        "customer_request": {
            "claimed_order_id": "ord_ref_fail",
            "claims": [{"claim_id": "c3", "topic": "refund_failed"}],
        },
    }
    refund_resp = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_ref_fail_" + "3" * 20,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "refund",
        "data": {
            "status": "failed",
            "failure_reason": "GATEWAY_TIMEOUT",
        },
    }

    ctx = make_context(tmp_path, case, {"get_refund_timeline": refund_resp})
    agent = PaymentRefundAgent()
    result = asyncio.run(agent.run(ctx))

    assert "refund_failed" in result.issue_signals
    assert result.issue_signals["refund_failed"] > 0.8
    assert "retry_refund" in result.actions


def test_missing_payment_evidence_does_not_become_payment_mismatch(tmp_path: Path) -> None:
    case = {"case_id": "CASE_MISSING_PAY", "customer_request": {"claimed_order_id": "ord_1"}}
    ctx = make_context(tmp_path, case, {})
    ctx.prior["order-agent"] = SpecialistResult(
        agent="order-agent",
        entities={"order_ids": ["ord_1"]},
        notes={"items_total_brl": 100.0},
    )
    result = asyncio.run(PaymentRefundAgent().run(ctx))
    assert "payment_mismatch" not in result.issue_signals


def test_timeline_uses_captures_within_case_window(tmp_path: Path) -> None:
    case = {
        "case_id": "CASE_SPLIT",
        "opened_at": "2018-05-05T09:00:00-03:00",
        "customer_request": {"claimed_order_id": "ord_1"},
    }
    responses = {
        "get_order_payments": {
            "evidence_ref": "ev_" + "1" * 24,
            "data": [
                {"payment_value": "44.50"},
                {"payment_value": "44.50"},
                {"payment_value": "52.00"},
            ],
        },
        "get_payment_timeline": {
            "evidence_ref": "ev_" + "2" * 24,
            "data": {
                "events": [
                    {
                        "event_at": "2018-04-23T10:00:00-03:00",
                        "event_type": "captured",
                        "status": "confirmed",
                        "amount_brl": "44.50",
                    },
                    {
                        "event_at": "2018-04-23T11:00:00-03:00",
                        "event_type": "captured",
                        "status": "confirmed",
                        "amount_brl": "44.50",
                    },
                    {
                        "event_at": "2018-01-07T10:00:00-03:00",
                        "event_type": "captured",
                        "status": "confirmed",
                        "amount_brl": "52.00",
                    },
                ]
            },
        },
        "get_refund_timeline": {
            "evidence_ref": "ev_" + "3" * 24,
            "data": {
                "events": [
                    {
                        "event_at": "2018-01-07T10:00:00-03:00",
                        "event_type": "refund_requested",
                        "status": "failed",
                    }
                ]
            },
        },
    }
    ctx = make_context(tmp_path, case, responses)
    ctx.prior["order-agent"] = SpecialistResult(
        agent="order-agent",
        entities={"order_ids": ["ord_1"]},
        notes={"order_value": 89.0, "order_purchase_at": "2018-04-23T09:00:00-03:00"},
    )
    result = asyncio.run(PaymentRefundAgent().run(ctx))

    assert result.strongest_issue() == "valid_split_payment"
    assert result.notes["total_paid"] == 89.0


def test_refund_status_comes_from_timeline_event(tmp_path: Path) -> None:
    case = {
        "case_id": "CASE_REFUND_EVENT",
        "opened_at": "2018-09-09T09:00:00-03:00",
        "customer_request": {"claimed_order_id": "ord_1"},
    }
    responses = {
        "get_refund_timeline": {
            "evidence_ref": "ev_" + "3" * 24,
            "data": {
                "events": [
                    {
                        "event_at": "2018-09-08T09:00:00-03:00",
                        "event_type": "refund_requested",
                        "status": "failed",
                        "amount_brl": "52.00",
                    }
                ]
            },
        }
    }
    result = asyncio.run(PaymentRefundAgent().run(make_context(tmp_path, case, responses)))

    assert result.strongest_issue() == "refund_failed"
    assert result.details_for("refund_failed").refund_lines[0]["amount_brl"] == 52.0
