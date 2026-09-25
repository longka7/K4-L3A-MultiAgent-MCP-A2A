
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

from .contract import CaseContext, SpecialistResult

class ShipmentSellerAgent:
    name = "shipment-seller-agent"
    tools = frozenset({"get_shipment_summary", "get_sellers"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        result = SpecialistResult(agent=self.name)

        shipment_evidence = await ctx.gateway.call("get_shipment_summary")
        seller_evidence = await ctx.gateway.call("get_sellers")

        shipment_ref = shipment_evidence["evidence_ref"]
        seller_ref = seller_evidence["evidence_ref"]

        result.evidence_refs.extend([shipment_ref, seller_ref])

        shipments = self._records(shipment_evidence)
        sellers = self._records(seller_evidence)

        # The gateway evidence is the source of truth for entity IDs.
        result.entities["shipment_ids"] = self._collect_ids(
            shipments,
            "shipment_id",
        )
        result.entities["seller_ids"] = self._collect_ids(
            sellers,
            "seller_id",
        )

        # Some shipment responses may carry seller_id even when the seller
        # response has a different/wrapped shape.
        shipment_seller_ids = self._collect_ids(
            shipments,
            "seller_id",
        )
        result.entities["seller_ids"] = self._unique(
            result.entities["seller_ids"] + shipment_seller_ids
        )

        for shipment in shipments:
            issue = self._classify_delay(shipment)
            if issue is None:
                continue

            result.issue_signals[issue] = max(
                result.issue_signals.get(issue, 0.0),
                self._signal_strength(shipment),
            )

            shipment_id = shipment.get("shipment_id")
            seller_id = shipment.get("seller_id")

            evidence_refs = [shipment_ref]

            # If the shipment explicitly references a seller, use that seller
            # as the responsible party only for seller-side delay.
            if issue == "late_delivery_seller":
                result.responsible_parties.append(
                    {
                        "party_type": "seller",
                        "party_id": (
                            str(seller_id)
                            if seller_id is not None
                            else None
                        ),
                    }
                )
                result.root_causes.append("SELLER_HANDOFF_LATE")

            elif issue == "late_delivery_logistics":
                logistics_id = self._first(
                    shipment,
                    "logistics_provider_id",
                    "carrier_id",
                    "shipping_provider_id",
                )

                result.responsible_parties.append(
                    {
                        "party_type": "logistics_provider",
                        "party_id": (
                            str(logistics_id)
                            if logistics_id is not None
                            else None
                        ),
                    }
                )
                result.root_causes.append("LOGISTICS_DELIVERY_LATE")

            claim_id = (
                f"{issue}:{shipment_id}"
                if shipment_id is not None
                else issue
            )

            result.claims.append(
                {
                    "claim_id": claim_id,
                    "verdict": issue,
                    "confidence": self._signal_strength(shipment),
                    "evidence_refs": evidence_refs,
                }
            )

        result.evidence_refs = self._unique(result.evidence_refs)
        result.root_causes = self._unique(result.root_causes)
        result.responsible_parties = self._unique_dicts(
            result.responsible_parties
        )

        result.notes["shipment_count"] = len(shipments)
        result.notes["seller_count"] = len(sellers)

        return result

    @staticmethod
    def _classify_delay(shipment: dict[str, Any]) -> str | None:
        """
        Distinguish seller delay from logistics delay using only fields
        present in MCP evidence.

        Explicit attribution takes precedence over timeline inference.
        """

        # Explicit attribution supplied by the gateway.
        cause = shipment.get("delay_cause")
        responsibility = shipment.get("responsible_party")
        delay_responsibility = shipment.get("delay_responsibility")

        explicit = " ".join(
            str(value).lower()
            for value in (
                cause,
                responsibility,
                delay_responsibility,
            )
            if value is not None
        )

        if shipment.get("seller_at_fault") is True:
            return "late_delivery_seller"

        if shipment.get("logistics_at_fault") is True:
            return "late_delivery_logistics"

        if "seller" in explicit or "merchant" in explicit:
            return "late_delivery_seller"

        if (
            "logistics" in explicit
            or "carrier" in explicit
            or "shipping_provider" in explicit
        ):
            return "late_delivery_logistics"

        # Explicit boolean timeline facts.
        if shipment.get("handoff_late") is True:
            return "late_delivery_seller"

        if (
            shipment.get("delivery_late") is True
            and shipment.get("handoff_late") is False
        ):
            return "late_delivery_logistics"

        return None

    @staticmethod
    def _signal_strength(shipment: dict[str, Any]) -> float:
        if (
            shipment.get("seller_at_fault") is True
            or shipment.get("logistics_at_fault") is True
            or shipment.get("delay_cause") is not None
            or shipment.get("responsible_party") is not None
            or shipment.get("delay_responsibility") is not None
        ):
            return 0.9

        if (
            shipment.get("handoff_late") is not None
            or shipment.get("delivery_late") is not None
        ):
            return 0.75

        return 0.5

    @staticmethod
    def _records(evidence: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract records while keeping the agent tolerant of common gateway
        wrapper shapes.
        """
        for key in (
            "shipments",
            "shipment",
            "sellers",
            "seller",
            "items",
            "results",
            "data",
        ):
            value = evidence.get(key)

            if isinstance(value, list):
                return [
                    item for item in value
                    if isinstance(item, dict)
                ]

            if isinstance(value, dict):
                return [value]

        # The evidence itself may represent one record.
        if any(
            key in evidence
            for key in (
                "shipment_id",
                "seller_id",
                "delay_cause",
                "delivery_late",
            )
        ):
            return [evidence]

        return []

    @staticmethod
    def _collect_ids(
        records: list[dict[str, Any]],
        key: str,
    ) -> list[str]:
        result: list[str] = []

        for record in records:
            value = record.get(key)
            if value is None:
                continue

            value = str(value)
            if value not in result:
                result.append(value)

        return result

    @staticmethod
    def _first(
        record: dict[str, Any],
        *keys: str,
    ) -> Any:
        for key in keys:
            value = record.get(key)
            if value is not None:
                return value
        return None

    @staticmethod
    def _unique(items: list[Any]) -> list[Any]:
        result: list[Any] = []
        seen: set[str] = set()

        for item in items:
            marker = repr(item)
            if marker not in seen:
                seen.add(marker)
                result.append(item)

        return result

    @staticmethod
    def _unique_dicts(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any]] = set()

        for item in items:
            key = (
                item.get("party_type"),
                item.get("party_id"),
            )

            if key not in seen:
                seen.add(key)
                result.append(item)

        return result


def _after(actual: Any, expected: Any) -> bool:
    """
    Compare ISO timestamps without inventing a timezone.

    Invalid/unparseable timestamps are treated as unavailable evidence.
    """
    try:
        actual_dt = datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
        expected_dt = datetime.fromisoformat(
            str(expected).replace("Z", "+00:00")
        )
        return actual_dt > expected_dt
    except (TypeError, ValueError):
        return False