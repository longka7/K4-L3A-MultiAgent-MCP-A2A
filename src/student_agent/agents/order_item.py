"""Người 2 — Order · Item agent.

Tools: get_order, get_order_items, get_product_context.
Nhận diện: canceled_order_paid, unavailable_order_paid. Điền entities order_ids/item_ids.

Kết luận theo issue đặt trong `issue_details[issue] = IssueDetail(...)`: coordinator chỉ giữ
phần của issue thắng. Vocabulary chuẩn: xem docstring của policy_verifier.py.
"""

from __future__ import annotations

from .contract import CaseContext, SpecialistResult


class OrderItemAgent:
    name = "order-item-agent"
    tools = frozenset({"get_order", "get_order_items", "get_product_context"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        # TODO(Người 2): gọi ctx.gateway.call("get_order", order_id=...) rồi điền kết quả.
        return SpecialistResult(agent=self.name)
