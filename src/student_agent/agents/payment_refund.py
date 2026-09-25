"""Người 4 — Payment · Refund agent.

Tools: get_order_payments, get_payment_timeline, get_refund_timeline.
Nhận diện: valid_split_payment, payment_mismatch, duplicate_charge, refund_pending,
refund_failed. Tính refund_lines (BRL, tổng phải khớp) và payment_references.

Kết luận theo issue đặt trong `issue_details[issue] = IssueDetail(...)`: coordinator chỉ giữ
phần của issue thắng. Vocabulary chuẩn: xem docstring của policy_verifier.py.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import Any

from .contract import CaseContext, IssueDetail, SpecialistResult, ToolFailure


def _within_window(event_at: Any, start: Any, end: Any) -> bool:
    if not event_at:
        return False
    try:
        event = datetime.fromisoformat(str(event_at).replace("Z", "+00:00"))
        if start and event < datetime.fromisoformat(str(start).replace("Z", "+00:00")):
            return False
        return not end or event <= datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False


class PaymentRefundAgent:
    name = "payment-refund-agent"
    tools = frozenset({"get_order_payments", "get_payment_timeline", "get_refund_timeline"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        result = SpecialistResult(agent=self.name)
        order_id = ctx.claimed_order_id

        # Nếu agent trước (order_item_agent / order-agent) đã tìm thấy order_ids, ưu tiên lấy
        prior_order = ctx.prior.get("order-item-agent") or ctx.prior.get("order-agent")
        if prior_order and prior_order.entities.get("order_ids"):
            order_id = prior_order.entities["order_ids"][0]

        if not order_id:
            return result

        # 1. Gọi MCP get_order_payments
        payments_data: Any = {}
        payments_loaded = False
        try:
            payments_evidence = await ctx.gateway.call("get_order_payments", order_id=order_id)
            result.evidence_refs.append(payments_evidence["evidence_ref"])
            payments_data = payments_evidence.get("data", {})
            payments_loaded = True
        except (ToolFailure, Exception):
            pass

        # 2. Gọi MCP get_payment_timeline
        payment_timeline_data: Any = {}
        try:
            pt_evidence = await ctx.gateway.call("get_payment_timeline", order_id=order_id)
            result.evidence_refs.append(pt_evidence["evidence_ref"])
            payment_timeline_data = pt_evidence.get("data", {})
        except (ToolFailure, Exception):
            pass

        # 3. Gọi MCP get_refund_timeline
        refund_timeline_data: Any = {}
        try:
            rt_evidence = await ctx.gateway.call("get_refund_timeline", order_id=order_id)
            result.evidence_refs.append(rt_evidence["evidence_ref"])
            refund_timeline_data = rt_evidence.get("data", {})
        except (ToolFailure, Exception):
            pass

        # Trích xuất payment_references và thông tin thanh toán
        payment_list: list[Any] = []
        if isinstance(payments_data, list):
            payment_list = payments_data
        elif isinstance(payments_data, dict):
            payment_list = payments_data.get("payments", [])
            if not isinstance(payment_list, list):
                payment_list = []

        payment_refs: list[str] = []
        total_paid = 0.0

        for p in payment_list:
            if isinstance(p, dict):
                ref = (
                    p.get("payment_reference")
                    or p.get("payment_id")
                    or p.get("reference")
                )
                if ref is not None:
                    payment_refs.append(str(ref))
                val = p.get("payment_value") or p.get("amount") or 0.0
                with suppress(ValueError, TypeError):
                    total_paid += float(val)

        # Bổ sung từ timeline nếu có
        if not isinstance(payment_timeline_data, dict):
            payment_timeline_data = {}
        events = payment_timeline_data.get("events", [])
        payment_count = len(payment_list)
        if isinstance(events, list) and events:
            purchase_at = prior_order.notes.get("order_purchase_at") if prior_order else None
            opened_at = ctx.case.get("opened_at")
            captures = [
                event
                for event in events
                if isinstance(event, dict)
                and event.get("event_type") == "captured"
                and event.get("status") == "confirmed"
                and _within_window(event.get("event_at"), purchase_at, opened_at)
            ]
            payment_count = len(captures)
            total_paid = 0.0
            for event in captures:
                with suppress(TypeError, ValueError):
                    total_paid += float(event.get("amount_brl") or 0)
                if event.get("payment_reference"):
                    payment_refs.append(str(event["payment_reference"]))

        if payment_refs:
            result.entities["payment_references"] = sorted(set(payment_refs))

        # Phân tích các issue nghiệp vụ thanh toán
        order_value = None
        if isinstance(payments_data, dict):
            order_value = payments_data.get("order_value") or payments_data.get("expected_amount")
        if order_value is None and prior_order:
            order_value = prior_order.notes.get("order_value") or prior_order.notes.get(
                "items_total_brl"
            )

        order_val_float = None
        if order_value is not None:
            with suppress(ValueError, TypeError):
                order_val_float = float(order_value)

        # 1. Kiểm tra duplicate_charge: có nhiều giao dịch hoặc tổng thanh toán vượt quá order_value
        is_duplicate = False
        duplicate_amount = 0.0
        if (
            payment_count > 1
            and order_val_float is not None
            and round(total_paid, 2) > round(order_val_float, 2)
        ):
            duplicate_amount = round(total_paid - order_val_float, 2)
            is_duplicate = True

        # Kiểm tra qua refund timeline
        if not isinstance(refund_timeline_data, dict):
            refund_timeline_data = {}
        refund_events = refund_timeline_data.get("events", [])
        refund_event: dict[str, Any] = {}
        if isinstance(refund_events, list):
            eligible = [
                event
                for event in refund_events
                if isinstance(event, dict)
                and event.get("event_type") == "refund_requested"
                and _within_window(
                    event.get("event_at"),
                    prior_order.notes.get("order_purchase_at") if prior_order else None,
                    ctx.case.get("opened_at"),
                )
            ]
            if eligible:
                refund_event = max(eligible, key=lambda event: str(event.get("event_at", "")))
        refund_status = (
            refund_event.get("status")
            or refund_timeline_data.get("status")
            or refund_timeline_data.get("refund_status")
            or ""
        ).lower()

        issue_details: dict[str, IssueDetail] = {}
        provider_id = (
            payments_data.get("provider_id") or payments_data.get("gateway_id")
            if isinstance(payments_data, dict)
            else None
        )
        provider = {"party_type": "payment_provider", "party_id": provider_id}

        # Đánh giá các tín hiệu (signals)
        if "failed" in refund_status or refund_timeline_data.get("failure_reason"):
            result.issue_signals["refund_failed"] = 0.95
            rc = ["REFUND_PROCESSING_FAILED"]
            resp = [provider]
            acts = ["retry_refund"]
            refund_amount = refund_event.get("amount_brl")
            if refund_amount is None:
                refund_amount = refund_timeline_data.get("refund_amount")
            if refund_amount is None:
                refund_amount = refund_timeline_data.get("amount_brl")
            try:
                ref_amt = float(refund_amount)
            except (TypeError, ValueError):
                ref_amt = 0.0
            rl = (
                [{"reason_code": "REFUND_RETRY", "amount_brl": ref_amt, "entity_id": order_id}]
                if ref_amt > 0
                else []
            )
            result.root_causes.extend(rc)
            result.responsible_parties.extend(resp)
            result.actions.extend(acts)
            result.refund_lines.extend(rl)
            issue_details["refund_failed"] = IssueDetail(
                root_causes=rc,
                responsible_parties=resp,
                refund_lines=rl,
                actions=acts,
            )
        elif "pending" in refund_status or "processing" in refund_status:
            result.issue_signals["refund_pending"] = 0.90
            rc = ["REFUND_PROCESSING_PENDING"]
            resp = [provider]
            acts = ["monitor_refund"]
            result.root_causes.extend(rc)
            result.responsible_parties.extend(resp)
            result.actions.extend(acts)
            issue_details["refund_pending"] = IssueDetail(
                root_causes=rc,
                responsible_parties=resp,
                refund_lines=[],
                actions=acts,
            )
        elif is_duplicate:
            result.issue_signals["duplicate_charge"] = 0.90
            rc = ["DUPLICATE_PAYMENT_CAPTURED"]
            resp = [provider]
            acts = ["refund_duplicate_charge"]
            rl = [
                {
                    "reason_code": "DUPLICATE_PAYMENT",
                    "amount_brl": duplicate_amount,
                    "entity_id": payment_refs[0] if payment_refs else order_id,
                }
            ] if duplicate_amount > 0 else []
            result.root_causes.extend(rc)
            result.responsible_parties.extend(resp)
            result.actions.extend(acts)
            result.refund_lines.extend(rl)
            issue_details["duplicate_charge"] = IssueDetail(
                root_causes=rc,
                responsible_parties=resp,
                refund_lines=rl,
                actions=acts,
            )
        elif (
            payment_count > 1
            and order_val_float is not None
            and abs(total_paid - order_val_float) < 0.05
        ):
            # Thanh toán chia làm nhiều đợt nhưng tổng khớp chính xác với order_value
            result.issue_signals["valid_split_payment"] = 0.85
            rc = ["CUSTOMER_SPLIT_PAYMENT_VALID"]
            resp = [{"party_type": "customer", "party_id": None}]
            acts = ["document_no_action"]
            result.root_causes.extend(rc)
            result.responsible_parties.extend(resp)
            result.actions.extend(acts)
            issue_details["valid_split_payment"] = IssueDetail(
                root_causes=rc,
                responsible_parties=resp,
                refund_lines=[],
                actions=acts,
            )
        elif (
            payments_loaded
            and payment_count > 0
            and order_val_float is not None
            and abs(total_paid - order_val_float) >= 0.05
        ):
            # Lệch tiền thanh toán
            result.issue_signals["payment_mismatch"] = 0.85
            diff = round(abs(total_paid - order_val_float), 2)
            rc = ["PAYMENT_AMOUNT_MISMATCH"]
            resp = [provider]
            acts = ["reconcile_payment"]
            rl = [
                {
                    "reason_code": "OVERPAID_AMOUNT",
                    "amount_brl": diff,
                    "entity_id": order_id,
                }
            ] if total_paid > order_val_float else []
            result.root_causes.extend(rc)
            result.responsible_parties.extend(resp)
            result.actions.extend(acts)
            result.refund_lines.extend(rl)
            issue_details["payment_mismatch"] = IssueDetail(
                root_causes=rc,
                responsible_parties=resp,
                refund_lines=rl,
                actions=acts,
            )

        result.notes["total_paid"] = round(total_paid, 2)
        if order_val_float is not None:
            result.notes["order_value"] = round(order_val_float, 2)
        result.issue_details = issue_details

        return result
