"""Người 4 — Payment · Refund agent.

Tools: get_order_payments, get_payment_timeline, get_refund_timeline.
Nhận diện: valid_split_payment, payment_mismatch, duplicate_charge, refund_pending,
refund_failed. Tính refund_lines (BRL, tổng phải khớp) và payment_references.

Kết luận theo issue đặt trong `issue_details[issue] = IssueDetail(...)`: coordinator chỉ giữ
phần của issue thắng. Vocabulary chuẩn: xem docstring của policy_verifier.py.
"""

from __future__ import annotations

from typing import Any

from .contract import CaseContext, SpecialistResult, ToolFailure


class PaymentRefundAgent:
    name = "payment-refund-agent"
    tools = frozenset({"get_order_payments", "get_payment_timeline", "get_refund_timeline"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        result = SpecialistResult(agent=self.name)
        order_id = ctx.claimed_order_id

        # Nếu case trước (order_item_agent) đã tìm thấy order_ids, ưu tiên lấy order_id đầu tiên
        prior_order = ctx.prior.get("order-item-agent")
        if prior_order and prior_order.entities.get("order_ids"):
            order_id = prior_order.entities["order_ids"][0]

        if not order_id:
            return result

        # 1. Gọi MCP get_order_payments
        payments_data: dict[str, Any] = {}
        try:
            payments_evidence = await ctx.gateway.call("get_order_payments", order_id=order_id)
            result.evidence_refs.append(payments_evidence["evidence_ref"])
            payments_data = payments_evidence.get("data", {})
        except (ToolFailure, Exception):
            pass

        # 2. Gọi MCP get_payment_timeline
        payment_timeline_data: dict[str, Any] = {}
        try:
            pt_evidence = await ctx.gateway.call("get_payment_timeline", order_id=order_id)
            result.evidence_refs.append(pt_evidence["evidence_ref"])
            payment_timeline_data = pt_evidence.get("data", {})
        except (ToolFailure, Exception):
            pass

        # 3. Gọi MCP get_refund_timeline
        refund_timeline_data: dict[str, Any] = {}
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
                    or p.get("payment_sequential")
                )
                if ref is not None:
                    payment_refs.append(str(ref))
                val = p.get("payment_value") or p.get("amount") or 0.0
                try:
                    total_paid += float(val)
                except (ValueError, TypeError):
                    pass

        # Bổ sung từ timeline nếu có
        for event in payment_timeline_data.get("events", []):
            if isinstance(event, dict) and event.get("payment_reference"):
                payment_refs.append(str(event["payment_reference"]))

        if payment_refs:
            result.entities["payment_references"] = sorted(set(payment_refs))

        # Phân tích các issue nghiệp vụ thanh toán
        # Lấy giá trị đơn hàng thực tế
        order_value = None
        if isinstance(payments_data, dict):
            order_value = payments_data.get("order_value") or payments_data.get("expected_amount")
        if order_value is None and prior_order:
            order_value = prior_order.notes.get("order_value")

        order_val_float = None
        if order_value is not None:
            try:
                order_val_float = float(order_value)
            except (ValueError, TypeError):
                pass

        # 1. Kiểm tra duplicate_charge: có nhiều giao dịch trùng giá trị hoặc tổng thanh toán vượt quá order_value
        is_duplicate = False
        duplicate_amount = 0.0
        if len(payment_list) > 1 and order_val_float is not None:
            if round(total_paid, 2) > round(order_val_float, 2):
                duplicate_amount = round(total_paid - order_val_float, 2)
                is_duplicate = True

        # Kiểm tra qua payment timeline hoặc refund timeline
        refund_status = (
            refund_timeline_data.get("status")
            or refund_timeline_data.get("refund_status")
            or ""
        ).lower()

        # Đánh giá các tín hiệu (signals)
        if "failed" in refund_status or refund_timeline_data.get("failure_reason"):
            result.issue_signals["refund_failed"] = 0.95
            result.root_causes.append("REFUND_PROCESSING_FAILED")
            result.responsible_parties.append({"party_type": "payment_provider", "party_id": None})
            result.actions.append("retry_refund")
        elif "pending" in refund_status or "processing" in refund_status:
            result.issue_signals["refund_pending"] = 0.9
            result.root_causes.append("REFUND_PROCESSING_PENDING")
            result.responsible_parties.append({"party_type": "payment_provider", "party_id": None})
            result.actions.append("monitor_refund_status")
        elif is_duplicate:
            result.issue_signals["duplicate_charge"] = 0.9
            result.root_causes.append("DUPLICATE_PAYMENT_CAPTURED")
            result.responsible_parties.append({"party_type": "payment_provider", "party_id": None})
            result.actions.append("refund_duplicate_charge")
            if duplicate_amount > 0:
                result.refund_lines.append(
                    {
                        "reason_code": "DUPLICATE_PAYMENT",
                        "amount_brl": duplicate_amount,
                        "entity_id": payment_refs[0] if payment_refs else order_id,
                    }
                )
        elif len(payment_list) > 1 and order_val_float is not None and abs(total_paid - order_val_float) < 0.05:
            # Thanh toán chia làm nhiều đợt nhưng tổng khớp chính xác với order_value
            result.issue_signals["valid_split_payment"] = 0.85
            result.root_causes.append("CUSTOMER_SPLIT_PAYMENT_VALID")
            result.responsible_parties.append({"party_type": "customer", "party_id": None})
        elif order_val_float is not None and abs(total_paid - order_val_float) >= 0.05:
            # Lệch tiền thanh toán
            result.issue_signals["payment_mismatch"] = 0.85
            diff = round(abs(total_paid - order_val_float), 2)
            result.root_causes.append("PAYMENT_AMOUNT_MISMATCH")
            result.responsible_parties.append({"party_type": "platform", "party_id": None})
            result.actions.append("reconcile_payment")
            if total_paid > order_val_float:
                result.refund_lines.append(
                    {
                        "reason_code": "OVERPAID_AMOUNT",
                        "amount_brl": diff,
                        "entity_id": order_id,
                    }
                )

        result.notes["total_paid"] = round(total_paid, 2)
        if order_val_float is not None:
            result.notes["order_value"] = round(order_val_float, 2)

        return result
