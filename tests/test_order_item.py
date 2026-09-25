from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from student_agent.agents.contract import CaseContext, ScopedGateway, ToolFailure
from student_agent.agents.order_item import OrderItemAgent
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import run_case

ROOT = Path(__file__).resolve().parents[1]
REF_ORDER = "ev_" + "1" * 24
REF_ITEMS = "ev_" + "2" * 24
REF_PROD = "ev_" + "3" * 24


class MockGateway:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if tool_name in self.responses:
            resp = self.responses[tool_name]
            if isinstance(resp, Exception):
                raise resp
            return resp

        if tool_name == "get_order":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": REF_ORDER,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": {
                    "order_id": arguments.get("order_id", "ORD_001"),
                    "order_status": "canceled",
                    "customer_id": "CUST_001",
                },
            }
        if tool_name == "get_order_items":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": REF_ITEMS,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "item",
                "data": {
                    "items": [
                        {
                            "order_id": arguments.get("order_id", "ORD_001"),
                            "order_item_id": 1,
                            "product_id": "PROD_ABC",
                            "seller_id": "SELLER_XYZ",
                            "price": 89.90,
                            "freight_value": 15.10,
                        }
                    ]
                },
            }
        if tool_name == "get_product_context":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": REF_PROD,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "product",
                "data": {"product_id": arguments.get("product_id"), "category": "electronics"},
            }
        raise RuntimeError(f"Unknown tool: {tool_name}")


def make_scoped_ctx(
    tmp_path: Path,
    case: dict[str, Any],
    mock_gw: MockGateway,
    agent: OrderItemAgent,
) -> CaseContext:
    trace_path = tmp_path / "trace.jsonl"
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(trace_path, contracts)
    scoped_gw = ScopedGateway(
        mock_gw,  # type: ignore[arg-type]
        case_id=case["case_id"],
        actor=agent.name,
        allowed_tools=agent.tools,
        trace=trace,
        ledger={},
        cache={},
        backoff=0,
    )
    return CaseContext(case, scoped_gw, trace, {})


def test_order_item_missing_order_id(tmp_path: Path) -> None:
    agent = OrderItemAgent()
    case = {"case_id": "CASE_NO_ORDER"}
    ctx = make_scoped_ctx(tmp_path, case, MockGateway(), agent)

    res = asyncio.run(agent.run(ctx))
    assert res.agent == "order-agent"
    assert res.issue_signals.get("insufficient_evidence") == 0.8
    assert res.notes.get("error") == "missing_order_id"


def test_order_item_canceled_order_flow(tmp_path: Path) -> None:
    agent = OrderItemAgent()
    case = {
        "case_id": "CASE_CANCELED",
        "customer_request": {
            "claimed_order_id": "ORD_001",
            "claimed_issue": "canceled_order",
        },
    }
    mock_gw = MockGateway()
    ctx = make_scoped_ctx(tmp_path, case, mock_gw, agent)

    res = asyncio.run(agent.run(ctx))
    assert res.agent == "order-agent"
    assert res.issue_signals["canceled_order_paid"] == 0.90
    assert REF_ORDER in res.evidence_refs
    assert REF_ITEMS in res.evidence_refs
    assert res.entities["order_ids"] == ["ORD_001"]
    assert "PROD_ABC" in res.entities["item_ids"]
    assert "1" in res.entities["item_ids"]
    assert "SELLER_XYZ" in res.entities["seller_ids"]
    assert "ORDER_CANCELED_BEFORE_FULFILLMENT" in res.root_causes
    assert len(res.refund_lines) == 1
    assert res.refund_lines[0]["amount_brl"] == pytest.approx(105.00, abs=0.01)
    assert res.notes["is_canceled"] is True
    assert res.notes["items_total_brl"] == pytest.approx(105.00, abs=0.01)


def test_order_item_unavailable_order_flow(tmp_path: Path) -> None:
    agent = OrderItemAgent()
    mock_gw = MockGateway(
        responses={
            "get_order": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": REF_ORDER,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": {"order_id": "ORD_UNAV", "order_status": "unavailable"},
            }
        }
    )
    case = {"case_id": "CASE_UNAVAILABLE", "customer_request": {"claimed_order_id": "ORD_UNAV"}}
    ctx = make_scoped_ctx(tmp_path, case, mock_gw, agent)

    res = asyncio.run(agent.run(ctx))
    assert res.issue_signals["unavailable_order_paid"] == 0.90
    assert "ORDER_ITEMS_UNAVAILABLE" in res.root_causes
    assert res.notes["is_unavailable"] is True


def test_order_item_conflict_detection(tmp_path: Path) -> None:
    agent = OrderItemAgent()
    mock_gw = MockGateway(
        responses={
            "get_order": {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": REF_ORDER,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": {"order_id": "ORD_DELIV", "order_status": "delivered"},
            }
        }
    )
    case = {
        "case_id": "CASE_CONFLICT",
        "customer_request": {
            "claimed_order_id": "ORD_DELIV",
            "claimed_issue": "canceled_order",
        },
    }
    ctx = make_scoped_ctx(tmp_path, case, mock_gw, agent)

    res = asyncio.run(agent.run(ctx))
    assert len(res.conflicts) == 1
    assert res.conflicts[0]["field"] == "order_status"
    assert res.conflicts[0]["selected_source"] == "mcp_get_order"


def test_order_item_gateway_failure_handling(tmp_path: Path) -> None:
    agent = OrderItemAgent()
    mock_gw = MockGateway(
        responses={"get_order": RuntimeError("MCP tool get_order failed: order not found")}
    )
    case = {"case_id": "CASE_ERR", "customer_request": {"claimed_order_id": "ORD_MISSING"}}
    ctx = make_scoped_ctx(tmp_path, case, mock_gw, agent)

    res = asyncio.run(agent.run(ctx))
    assert res.issue_signals.get("insufficient_evidence") == 0.8
    assert res.notes.get("order_lookup_failed") is True
    assert res.evidence_refs == []


def test_order_item_integration_with_workflow(tmp_path: Path) -> None:
    agent = OrderItemAgent()
    case = {
        "case_id": "CASE_INTEG",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {"claimed_order_id": "ORD_001"},
    }
    trace_path = tmp_path / "trace.jsonl"
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(trace_path, contracts)
    mock_gw = MockGateway()

    output = asyncio.run(
        run_case(
            case,
            mock_gw,  # type: ignore[arg-type]
            trace,
            [agent],
            lambda output, ctx: [],
        )
    )

    contracts.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["affected_entities"]["order_ids"] == ["ORD_001"]
    assert REF_ORDER in output["evidence_refs"]


def test_order_item_with_real_input_case_001(tmp_path: Path) -> None:
    case_path = ROOT / "inputs" / "L3A_CASE_001.json"
    if not case_path.exists():
        pytest.skip("Real inputs not present")

    import json
    case = json.loads(case_path.read_text(encoding="utf-8"))
    agent = OrderItemAgent()
    mock_gw = MockGateway()
    ctx = make_scoped_ctx(tmp_path, case, mock_gw, agent)

    res = asyncio.run(agent.run(ctx))
    assert res.agent == "order-agent"
    assert res.entities["order_ids"] == ["e2a03ccf5ea816036608b2d8c3ab8e60"]
    assert any(call[0] == "get_order" and call[1]["order_id"] == "e2a03ccf5ea816036608b2d8c3ab8e60" for call in mock_gw.calls)
    assert any(call[0] == "get_order_items" and call[1]["order_id"] == "e2a03ccf5ea816036608b2d8c3ab8e60" for call in mock_gw.calls)
    # Claims from real case L3A_CASE_001: claim-001-a (canceled_order_paid), claim-001-b (requested_full_refund)
    claim_ids = [c["claim_id"] for c in res.claims]
    assert "claim-001-a" in claim_ids
    assert "claim-001-b" in claim_ids


def test_all_100_real_inputs_order_id_extractable() -> None:
    input_dir = ROOT / "inputs"
    input_files = list(input_dir.glob("L3A_CASE_*.json"))
    if not input_files:
        pytest.skip("Real inputs not present")

    import json
    for file_path in input_files:
        case = json.loads(file_path.read_text(encoding="utf-8"))
        claimed = case.get("customer_request", {}).get("claimed_order_id")
        assert claimed is not None and len(claimed) == 32, f"Failed on {file_path.name}"

