"""Người 3 — Shipment · Seller agent.

Tools: get_shipment_summary, get_sellers.
Phân biệt: late_delivery_seller vs late_delivery_logistics. Điền responsible_parties,
entities seller_ids/shipment_ids.
"""

from __future__ import annotations

from .contract import CaseContext, SpecialistResult


class ShipmentSellerAgent:
    name = "shipment-seller-agent"
    tools = frozenset({"get_shipment_summary", "get_sellers"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        # TODO(Người 3): gọi get_shipment_summary / get_sellers rồi điền kết quả.
        return SpecialistResult(agent=self.name)
