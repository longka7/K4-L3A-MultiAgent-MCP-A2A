"""Người 5 — Policy agent + Verifier.

Policy agent tools: get_policy, get_customer_history.
Điền claims (claim_assessments) và nhận diện unsupported_claim / insufficient_evidence.
Nhớ phát trace `policy_decided` khi có quyết định theo policy.

verify(): kiểm invariant trước khi finalize; trả về danh sách mã lỗi (rỗng = đạt).
Coordinator đã tự kiểm: case_id khớp, evidence_refs thuộc ledger MCP của đúng case.
"""

from __future__ import annotations

from typing import Any

from .contract import CaseContext, SpecialistResult


class PolicyAgent:
    name = "policy-agent"
    tools = frozenset({"get_policy", "get_customer_history"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        # TODO(Người 5): gọi get_policy(policy_version=ctx.policy_version) và đánh giá claims.
        return SpecialistResult(agent=self.name)


def verify(output: dict[str, Any], ctx: CaseContext) -> list[str]:
    """TODO(Người 5): thêm kiểm tra tổng tiền, status↔refund↔action, seller responsibility."""
    return []
