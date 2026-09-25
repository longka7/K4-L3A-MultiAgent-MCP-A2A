"""Người 3 — Shipment · Seller agent.

Tools: get_shipment_summary, get_sellers.
Phân biệt: late_delivery_seller vs late_delivery_logistics. Điền responsible_parties,
entities seller_ids/shipment_ids.

Kết luận theo issue đặt trong `issue_details[issue] = IssueDetail(...)`: coordinator chỉ giữ
phần của issue thắng. party_id của seller phải lấy từ evidence, không dùng id mẫu của policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contract import CaseContext, IssueDetail, SpecialistResult, ToolFailure


def _records(data: Any, plural: str, singular: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in (plural, singular, "items", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    return [data]


def _later(actual: Any, expected: Any) -> bool:
    if not actual or not expected:
        return False
    try:
        actual_dt = datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
        expected_dt = datetime.fromisoformat(str(expected).replace("Z", "+00:00"))
        return actual_dt > expected_dt
    except (TypeError, ValueError):
        return False


def _first(record: dict[str, Any], *keys: str) -> Any:
    return next((record[key] for key in keys if record.get(key) is not None), None)


class ShipmentSellerAgent:
    name = "shipment-seller-agent"
    tools = frozenset({"get_shipment_summary", "get_sellers"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        result = SpecialistResult(agent=self.name)
        order_id = ctx.claimed_order_id

        prior_order = ctx.prior.get("order-item-agent") or ctx.prior.get("order-agent")
        if prior_order and prior_order.entities.get("order_ids"):
            order_id = prior_order.entities["order_ids"][0]

        if not order_id:
            return result

        # 1. Gọi get_shipment_summary
        shipment_data: Any = {}
        try:
            shipment_ev = await ctx.gateway.call("get_shipment_summary", order_id=order_id)
            result.evidence_refs.append(shipment_ev["evidence_ref"])
            shipment_data = shipment_ev.get("data", {})
        except (ToolFailure, Exception):
            pass

        shipments = _records(shipment_data, "shipments", "shipment")
        shipment_ids = [
            str(value)
            for shipment in shipments
            if (value := _first(shipment, "shipment_id", "tracking_number", "package_id"))
        ]
        if shipment_ids:
            result.entities["shipment_ids"] = list(dict.fromkeys(shipment_ids))[:20]

        # 2. Xác định seller_id từ prior hoặc gọi get_sellers
        prior_sellers = prior_order.entities.get("seller_ids", []) if prior_order else []
        seller_ids = list(prior_sellers)
        seller_ids.extend(
            str(shipment["seller_id"])
            for shipment in shipments
            if shipment.get("seller_id") is not None
        )
        for shipment in shipments:
            limits = shipment.get("shipping_limits")
            if isinstance(limits, list):
                seller_ids.extend(
                    str(limit["seller_id"])
                    for limit in limits
                    if isinstance(limit, dict) and limit.get("seller_id") is not None
                )

        if not seller_ids:
            try:
                seller_ev = await ctx.gateway.call("get_sellers", order_id=order_id)
                result.evidence_refs.append(seller_ev["evidence_ref"])
                sellers = _records(seller_ev.get("data"), "sellers", "seller")
                seller_ids.extend(
                    str(seller["seller_id"])
                    for seller in sellers
                    if seller.get("seller_id") is not None
                )
            except (ToolFailure, Exception):
                pass

        if seller_ids:
            result.entities["seller_ids"] = list(dict.fromkeys(seller_ids))[:20]

        # Explicit attribution wins over a timeline inference. A late delivery
        # alone cannot establish which party caused the delay.
        for shipment in shipments:
            limit_date = _first(
                shipment, "shipping_limit_date", "seller_shipping_limit_date", "limit_date"
            )
            limits = shipment.get("shipping_limits")
            if limit_date is None and isinstance(limits, list):
                distinct_limits = {
                    limit.get("shipping_limit_at")
                    for limit in limits
                    if isinstance(limit, dict) and limit.get("shipping_limit_at")
                }
                if len(distinct_limits) == 1:
                    limit_date = next(iter(distinct_limits))
            pickup_date = _first(
                shipment,
                "carrier_pickup_date",
                "order_delivered_carrier_date",
                "delivered_carrier_at",
                "pickup_date",
            )
            delivered_date = _first(
                shipment,
                "delivered_customer_date",
                "order_delivered_customer_date",
                "actual_delivery_date",
                "delivered_customer_at",
                "delivered_date",
            )
            estimated_date = _first(
                shipment,
                "estimated_delivery_date",
                "order_estimated_delivery_date",
                "estimated_delivery_at",
                "estimated_date",
            )
            attribution = " ".join(
                str(shipment.get(key, "")).lower()
                for key in ("delay_cause", "responsible_party", "delay_responsibility")
            )
            handoff_late = shipment.get("handoff_late") is True or _later(pickup_date, limit_date)
            delivery_late = shipment.get("delivery_late") is True or _later(
                delivered_date, estimated_date
            )
            explicit_seller = shipment.get("seller_at_fault") is True or any(
                term in attribution for term in ("seller", "merchant")
            )
            explicit_logistics = shipment.get("logistics_at_fault") is True or any(
                term in attribution for term in ("logistics", "carrier", "shipping_provider")
            )
            events = shipment.get("events")
            if isinstance(events, list):
                late_actors = {
                    str(event.get("actor"))
                    for event in events
                    if isinstance(event, dict)
                    and event.get("event_type") == "delivered_late"
                    and event.get("status") == "confirmed"
                }
                explicit_seller |= "seller" in late_actors
                explicit_logistics |= "logistics_provider" in late_actors
            logistics_timeline = delivery_late and (
                shipment.get("handoff_late") is False
                or (pickup_date and limit_date and not handoff_late)
            )
            if explicit_seller and explicit_logistics:
                continue
            if explicit_seller or (not explicit_logistics and handoff_late):
                issue, cause, party_type = "late_delivery_seller", "SELLER_HANDOFF_LATE", "seller"
                party_id = shipment.get("seller_id") or (seller_ids[0] if seller_ids else None)
            elif explicit_logistics or logistics_timeline:
                issue = "late_delivery_logistics"
                cause, party_type = "LOGISTICS_TRANSIT_DELAY", "logistics_provider"
                party_id = _first(
                    shipment,
                    "logistics_provider_id",
                    "carrier_id",
                    "shipping_provider_id",
                    "carrier_name",
                    "carrier",
                )
            else:
                continue

            explicit = explicit_seller or explicit_logistics
            result.issue_signals[issue] = max(
                result.issue_signals.get(issue, 0.0), 0.9 if explicit else 0.85
            )
            freight_source = shipment.get("freight_value")
            if freight_source is None and prior_order:
                freight_source = prior_order.notes.get("freight_total_brl")
            try:
                freight = float(freight_source or 0)
            except (TypeError, ValueError):
                freight = 0.0
            refund_lines = []
            if freight > 0:
                refund_lines.append(
                    {
                        "reason_code": (
                            "SELLER_LATE_DELIVERY"
                            if issue == "late_delivery_seller"
                            else "LOGISTICS_LATE_DELIVERY"
                        ),
                        "amount_brl": round(freight, 2),
                        "entity_id": order_id,
                    }
                )
            detail = result.issue_details.setdefault(issue, IssueDetail())
            detail.root_causes.append(cause)
            detail.responsible_parties.append(
                {
                    "party_type": party_type,
                    "party_id": str(party_id) if party_id is not None else None,
                }
            )
            detail.refund_lines.extend(refund_lines)
            detail.actions.append("refund_freight")

        result.notes["shipment_count"] = len(shipments)
        return result
