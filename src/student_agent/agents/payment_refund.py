"""Người 4 — Payment · Refund agent.

Tools: get_order_payments, get_payment_timeline, get_refund_timeline.
Nhận diện: valid_split_payment, payment_mismatch, duplicate_charge, refund_pending,
refund_failed. Tính refund_lines (BRL, tổng phải khớp) và payment_references.

Kết luận theo issue đặt trong `issue_details[issue] = IssueDetail(...)`: coordinator chỉ giữ
phần của issue thắng. Vocabulary chuẩn: xem docstring của policy_verifier.py.
"""

from __future__ import annotations

from .contract import CaseContext, SpecialistResult


class PaymentRefundAgent:
    name = "payment-refund-agent"
    tools = frozenset({"get_order_payments", "get_payment_timeline", "get_refund_timeline"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        # TODO(Người 4): gọi các tool payment/refund rồi điền kết quả.
        return SpecialistResult(agent=self.name)
