from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.agents.contract import CaseContext, ScopedGateway, SpecialistResult
from student_agent.agents.shipment_seller import ShipmentSellerAgent
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        return {
            "evidence_ref": "ev_" + "a" * 24,
            "data": self.data,
        }


def make_context(
    tmp_path: Path, data: Any, prior: SpecialistResult | None = None
) -> tuple[CaseContext, FakeGateway]:
    case = {"case_id": "CASE_SHIP", "customer_request": {"claimed_order_id": "order_1"}}
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    gateway = FakeGateway(data)
    scoped = ScopedGateway(
        gateway,  # type: ignore[arg-type]
        case_id=case["case_id"],
        actor=ShipmentSellerAgent.name,
        allowed_tools=ShipmentSellerAgent.tools,
        trace=trace,
        ledger={},
        cache={},
    )
    return CaseContext(case, scoped, trace, {prior.agent: prior} if prior else {}), gateway


def test_explicit_logistics_attribution_beats_timeline_and_does_not_guess_refund(
    tmp_path: Path,
) -> None:
    ctx, gateway = make_context(
        tmp_path,
        {
            "shipment_id": "ship_1",
            "seller_id": "seller_1",
            "carrier_id": "carrier_1",
            "handoff_late": True,
            "logistics_at_fault": True,
        },
    )
    result = asyncio.run(ShipmentSellerAgent().run(ctx))

    assert result.strongest_issue() == "late_delivery_logistics"
    assert result.entities["shipment_ids"] == ["ship_1"]
    assert result.entities["seller_ids"] == ["seller_1"]
    assert result.details_for("late_delivery_logistics").responsible_parties == [
        {"party_type": "logistics_provider", "party_id": "carrier_1"}
    ]
    assert result.details_for("late_delivery_logistics").refund_lines == []
    assert [name for name, _ in gateway.calls] == ["get_shipment_summary"]
    assert gateway.calls[0][1] == {"case_id": "CASE_SHIP", "order_id": "order_1"}


def test_late_seller_handoff_uses_evidence_freight_and_prior_seller(tmp_path: Path) -> None:
    prior = SpecialistResult(agent="order-agent", entities={"seller_ids": ["seller_2"]})
    ctx, gateway = make_context(
        tmp_path,
        {
            "shipment": {
                "shipment_id": "ship_2",
                "shipping_limit_date": "2024-01-01T00:00:00Z",
                "carrier_pickup_date": "2024-01-02T00:00:00Z",
                "freight_value": 12.5,
            }
        },
        prior,
    )
    result = asyncio.run(ShipmentSellerAgent().run(ctx))

    assert result.strongest_issue() == "late_delivery_seller"
    assert result.details_for("late_delivery_seller").responsible_parties == [
        {"party_type": "seller", "party_id": "seller_2"}
    ]
    assert result.details_for("late_delivery_seller").refund_lines == [
        {"reason_code": "SELLER_LATE_DELIVERY", "amount_brl": 12.5, "entity_id": "order_1"}
    ]
    assert [name for name, _ in gateway.calls] == ["get_shipment_summary"]
