"""Order · Item agent.

Tools: get_order, get_order_items, get_product_context.
Nhận diện: canceled_order_paid, unavailable_order_paid. Điền entities order_ids/item_ids.

Kết luận theo issue đặt trong `issue_details[issue] = IssueDetail(...)`: coordinator chỉ giữ
phần của issue thắng. Vocabulary chuẩn: xem docstring của policy_verifier.py.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import Any

from .contract import CaseContext, IssueDetail, ScopedGateway, SpecialistResult, ToolFailure


def _unique_strings(items: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    res: list[str] = []
    for item in items:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            res.append(s)
    return res


class OrderItemAgent:
    name = "order-agent"
    tools = frozenset({"get_order", "get_order_items", "get_product_context"})

    async def _call_tool(
        self, ctx: CaseContext, tool_name: str, **kwargs: Any
    ) -> tuple[dict[str, Any] | None, ToolFailure | None]:
        """Call MCP tool via gateway, supporting both ScopedGateway and raw EvidenceGateway."""
        if tool_name not in self.tools:
            return None, None

        try:
            if isinstance(ctx.gateway, ScopedGateway):
                kwargs.pop("case_id", None)
                evidence = await ctx.gateway.call(tool_name, **kwargs)
                return evidence, None
            else:
                kwargs.setdefault("case_id", ctx.case_id)
                evidence = await ctx.gateway.call(tool_name, **kwargs)
                ref = evidence.get("evidence_ref")
                if ref and ctx.trace:
                    ctx.trace.emit(
                        case_id=ctx.case_id,
                        event_type="tool_result_consumed",
                        actor=self.name,
                        tool_name=tool_name,
                        evidence_refs=[ref],
                    )
                return evidence, None
        except ToolFailure as tf:
            return None, tf
        except Exception as exc:
            return None, ToolFailure(tool_name, "unexpected_error", type(exc).__name__)

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        # 1. Trích xuất order_id từ ctx hoặc case
        order_id = (
            ctx.claimed_order_id
            or ctx.case.get("order_id")
            or ctx.case.get("customer_request", {}).get("order_id")
        )
        if not order_id:
            raw_ids = (
                ctx.case.get("order_ids")
                or ctx.case.get("customer_request", {}).get("order_ids")
            )
            if isinstance(raw_ids, list) and raw_ids:
                order_id = str(raw_ids[0])

        if not order_id:
            return SpecialistResult(
                agent=self.name,
                issue_signals={"insufficient_evidence": 0.8},
                notes={"error": "missing_order_id"},
            )

        order_id = str(order_id).strip()

        # 2. Truy vấn thông tin đơn hàng qua MCP tool get_order
        order_ev, order_err = await self._call_tool(ctx, "get_order", order_id=order_id)
        if order_ev is None:
            err_kind = order_err.kind if order_err else "unknown"
            return SpecialistResult(
                agent=self.name,
                issue_signals={"insufficient_evidence": 0.8},
                notes={
                    "order_lookup_failed": True,
                    "error_kind": err_kind,
                    "error_type": str(order_err)[:120] if order_err else "unknown",
                    "claimed_order_id": order_id,
                },
                root_causes=["INSUFFICIENT_ORDER_EVIDENCE"],
                actions=["request_customer_info"],
            )

        evidence_refs: list[str] = [order_ev["evidence_ref"]]
        order_data = order_ev.get("data", {})
        if not isinstance(order_data, dict):
            order_data = {}

        order_status = str(order_data.get("order_status", "")).strip().lower()

        # 3. Truy vấn danh sách item qua MCP tool get_order_items
        items_ev, _ = await self._call_tool(ctx, "get_order_items", order_id=order_id)
        items_data: Any = None
        if items_ev:
            evidence_refs.append(items_ev["evidence_ref"])
            items_data = items_ev.get("data")

        raw_items: list[Any] = []
        if isinstance(items_data, list):
            raw_items = items_data
        elif isinstance(items_data, dict):
            candidate = items_data.get("items") or items_data.get("order_items") or []
            if isinstance(candidate, list):
                raw_items = candidate

        item_ids: list[str] = []
        seller_ids: list[str] = []
        total_items_price = 0.0
        total_freight_value = 0.0
        priced_item_ids: set[str] = set()

        for it in raw_items:
            if not isinstance(it, dict):
                continue
            if "order_item_id" in it:
                item_ids.append(str(it["order_item_id"]))
            elif "item_id" in it:
                item_ids.append(str(it["item_id"]))

            if "seller_id" in it and it["seller_id"]:
                seller_ids.append(str(it["seller_id"]))

            # MCP may return another row for the same order item with different
            # values. Count a priced item once; keep the first observed row.
            priced_id = str(it.get("order_item_id") or it.get("item_id") or "")
            if priced_id and priced_id in priced_item_ids:
                continue
            if priced_id:
                priced_item_ids.add(priced_id)
            with suppress(ValueError, TypeError):
                total_items_price += float(it.get("price", 0.0))
            with suppress(ValueError, TypeError):
                total_freight_value += float(it.get("freight_value", 0.0))

        # 4. Truy vấn context sản phẩm nếu có claimed_product_id
        claimed_pid = (
            ctx.case.get("customer_request", {}).get("claimed_product_id")
            or ctx.case.get("product_id")
        )
        if claimed_pid:
            target_pid = str(claimed_pid).strip()
            prod_ev, _ = await self._call_tool(ctx, "get_product_context", product_id=target_pid)
            if prod_ev:
                evidence_refs.append(prod_ev["evidence_ref"])

        # 5. Xây dựng entities theo contract
        entities: dict[str, list[str]] = {
            "order_ids": [order_id],
            "item_ids": _unique_strings(item_ids)[:20],
        }
        if seller_ids:
            entities["seller_ids"] = _unique_strings(seller_ids)[:20]

        # 6. Đánh giá trạng thái & gắn tín hiệu nghiệp vụ
        issue_signals: dict[str, float] = {}
        root_causes: list[str] = []
        responsible_parties: list[dict[str, Any]] = []
        refund_lines: list[dict[str, Any]] = []
        actions: list[str] = []
        conflicts: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        issue_details: dict[str, IssueDetail] = {}

        total_order_amount = round(total_items_price + total_freight_value, 2)

        if order_status == "canceled":
            issue_signals["canceled_order_paid"] = 0.90
            root_causes.append("ORDER_CANCELED_BEFORE_FULFILLMENT")
            responsible_parties.append({"party_type": "platform", "party_id": None})
            if seller_ids:
                responsible_parties.append({"party_type": "seller", "party_id": seller_ids[0]})
            actions.extend(["verify_payment_status", "refund_customer"])
            if total_order_amount > 0:
                refund_lines.append({
                    "reason_code": "ORDER_CANCELED",
                    "amount_brl": total_order_amount,
                    "entity_id": order_id,
                })
            issue_details["canceled_order_paid"] = IssueDetail(
                root_causes=list(root_causes),
                responsible_parties=list(responsible_parties),
                refund_lines=list(refund_lines),
                actions=list(actions),
            )
        elif order_status == "unavailable":
            issue_signals["unavailable_order_paid"] = 0.90
            root_causes.append("ORDER_ITEMS_UNAVAILABLE")
            if seller_ids:
                responsible_parties.append({"party_type": "seller", "party_id": seller_ids[0]})
            else:
                responsible_parties.append({"party_type": "platform", "party_id": None})
            actions.extend(["notify_customer", "refund_customer"])
            if total_order_amount > 0:
                refund_lines.append({
                    "reason_code": "ORDER_UNAVAILABLE",
                    "amount_brl": total_order_amount,
                    "entity_id": order_id,
                })
            issue_details["unavailable_order_paid"] = IssueDetail(
                root_causes=list(root_causes),
                responsible_parties=list(responsible_parties),
                refund_lines=list(refund_lines),
                actions=list(actions),
            )

        # 7. Phát hiện xung đột dữ liệu giữa claim của khách hàng và MCP
        claimed_issue = ctx.case.get("customer_request", {}).get("claimed_issue")
        if claimed_issue in ("canceled_order", "order_canceled") and order_status not in (
            "canceled",
            "",
        ):
            conflicts.append({
                "field": "order_status",
                "sources": ["customer_claim", "mcp_get_order"],
                "selected_source": "mcp_get_order",
                "resolution_code": "RESOLVE_BY_MCP_EVIDENCE",
            })

        # 8. Đánh giá claim nếu có liên quan đến order/item
        raw_claims = (
            ctx.case.get("claims")
            or ctx.case.get("customer_request", {}).get("claims")
            or []
        )
        if isinstance(raw_claims, list):
            for cl in raw_claims:
                if isinstance(cl, dict) and "claim_id" in cl:
                    cid = str(cl["claim_id"])
                    topic = str(
                        cl.get("topic") or cl.get("claim_type") or cl.get("description") or ""
                    ).lower()
                    if "cancel" in topic:
                        verdict = "supported" if order_status == "canceled" else "unsupported"
                        claims.append({
                            "claim_id": cid,
                            "verdict": verdict,
                            "confidence": 0.95,
                            "evidence_refs": [order_ev["evidence_ref"]],
                        })
                    elif "unavailable" in topic:
                        verdict = "supported" if order_status == "unavailable" else "unsupported"
                        claims.append({
                            "claim_id": cid,
                            "verdict": verdict,
                            "confidence": 0.95,
                            "evidence_refs": [order_ev["evidence_ref"]],
                        })
                    elif "refund" in topic and order_status in ("canceled", "unavailable"):
                        claims.append({
                            "claim_id": cid,
                            "verdict": "supported",
                            "confidence": 0.90,
                            "evidence_refs": [order_ev["evidence_ref"]],
                        })

        # 9. Ghi nhận notes (dữ liệu trung gian sạch dạng primitive cho coordinator và agent sau)
        notes: dict[str, str | int | float | bool | None] = {
            "order_id": order_id,
            "order_status": order_status,
            "items_count": len(raw_items),
            "items_total_brl": total_order_amount,
            "order_value": total_order_amount,
            "freight_total_brl": round(total_freight_value, 2),
            "order_purchase_at": str(order_data.get("order_purchase_timestamp") or ""),
            "has_items": bool(raw_items),
            "seller_count": len(seller_ids),
            "is_canceled": order_status == "canceled",
            "is_unavailable": order_status == "unavailable",
        }

        return SpecialistResult(
            agent=self.name,
            issue_signals=issue_signals,
            entities=entities,
            evidence_refs=evidence_refs,
            claims=claims,
            root_causes=root_causes,
            responsible_parties=responsible_parties,
            refund_lines=refund_lines,
            conflicts=conflicts,
            actions=actions,
            notes=notes,
            issue_details=issue_details,
        )
