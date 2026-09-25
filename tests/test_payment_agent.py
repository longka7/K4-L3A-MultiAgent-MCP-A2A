from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

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
