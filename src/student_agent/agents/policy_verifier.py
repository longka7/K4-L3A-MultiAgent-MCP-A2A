"""Người 5 — Policy agent + Verifier.

Policy agent tools: get_policy, get_customer_history.
Điền claims (claim_assessments) và nhận diện unsupported_claim / insufficient_evidence.

get_policy trả `data["rules"]`: bảng theo từng issue với case_status, recommended_action,
refund_brl, responsible_parties. Đặt nguyên `rules` vào `SpecialistResult.policy_rules` và
thêm evidence_ref của get_policy vào `evidence_refs`. Khi có policy, coordinator dùng nó để
đặt case_status, resolution_actions (= recommended_action) và loại bên chịu trách nhiệm.
Vocabulary của EC_POLICY_V1: issue_refund, refund_duplicate_charge, refund_freight,
reconcile_payment, retry_refund, monitor_refund, document_no_action.
Lưu ý: bảng này giống hệt nhau giữa các case (đã kiểm ở case 001 và 002), nên refund_brl và
seller party_id là giá trị mẫu, KHÔNG chép thẳng làm số tiền/seller của case.
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
