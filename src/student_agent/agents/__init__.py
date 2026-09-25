"""Specialist agents. Thứ tự trong SPECIALISTS = thứ tự chạy (agent sau đọc được ctx.prior)."""

from __future__ import annotations

from .contract import Specialist
from .order_item import OrderItemAgent
from .payment_refund import PaymentRefundAgent
from .policy_verifier import PolicyAgent, verify
from .shipment_seller import ShipmentSellerAgent

SPECIALISTS: list[Specialist] = [
    OrderItemAgent(),
    ShipmentSellerAgent(),
    PaymentRefundAgent(),
    PolicyAgent(),
]

__all__ = ["SPECIALISTS", "verify"]
