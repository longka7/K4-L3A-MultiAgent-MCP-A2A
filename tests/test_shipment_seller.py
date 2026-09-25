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


def test_summary_event_actor_resolves_conflicting_shipping_limits(tmp_path: Path) -> None:
    prior = SpecialistResult(
        agent="order-agent",
        entities={"seller_ids": ["seller_3"]},
        notes={"freight_total_brl": 18.0},
    )
    ctx, gateway = make_context(
        tmp_path,
        {
            "delivered_carrier_at": "2018-02-26T09:00:00-03:00",
            "delivered_customer_at": "2018-03-05T09:00:00-03:00",
            "estimated_delivery_at": "2018-03-01T09:00:00-03:00",
            "shipping_limits": [
                {"seller_id": "seller_3", "shipping_limit_at": "2018-02-22T09:00:00-03:00"},
                {"seller_id": "seller_3", "shipping_limit_at": "2018-03-12T09:00:00-03:00"},
            ],
            "events": [
                {
                    "event_at": "2018-03-05T09:00:00-03:00",
                    "event_type": "delivered_late",
                    "actor": "seller",
                    "status": "confirmed",
                }
            ],
        },
        prior,
    )
    result = asyncio.run(ShipmentSellerAgent().run(ctx))

    assert result.strongest_issue() == "late_delivery_seller"
    assert result.details_for("late_delivery_seller").refund_lines[0]["amount_brl"] == 18.0
    assert [name for name, _ in gateway.calls] == ["get_shipment_summary"]


def test_late_event_after_early_delivery_is_ignored(tmp_path: Path) -> None:
    ctx, _ = make_context(
        tmp_path,
        {
            "delivered_carrier_at": "2017-12-31T09:00:00-03:00",
            "delivered_customer_at": "2018-01-07T09:00:00-03:00",
            "estimated_delivery_at": "2018-01-08T09:00:00-03:00",
            "events": [
                {
                    "event_at": "2018-05-17T09:00:00-03:00",
                    "event_type": "delivered_late",
                    "actor": "logistics_provider",
                    "status": "confirmed",
                }
            ],
        },
    )
    result = asyncio.run(ShipmentSellerAgent().run(ctx))

    assert "late_delivery_logistics" not in result.issue_signals
