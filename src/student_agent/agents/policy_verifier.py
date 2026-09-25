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

import re
from typing import Any

from .contract import (
    ISSUE_CODES,
    PARTY_TYPES,
    CaseContext,
    IssueDetail,
    SpecialistResult,
    ToolFailure,
)

CAUSE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
REFUND_JUSTIFYING_ISSUES = frozenset(
    {
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "payment_mismatch",
        "refund_failed",
    }
)
NO_ACTION_ISSUES = frozenset({"valid_split_payment", "unsupported_claim"})


class PolicyAgent:
    name = "policy-agent"
    tools = frozenset({"get_policy", "get_customer_history"})

    async def run(self, ctx: CaseContext) -> SpecialistResult:
        result = SpecialistResult(agent=self.name)
        policy_ver = ctx.policy_version or "EC_POLICY_V1"

        # 1. Gọi get_policy để lấy thông tin chính sách có thẩm quyền qua MCP
        try:
            policy_evidence = await ctx.gateway.call("get_policy", policy_version=policy_ver)
            ref = policy_evidence["evidence_ref"]
            result.evidence_refs.append(ref)
            policy_data = policy_evidence.get("data", {})
            if isinstance(policy_data, dict):
                rules = policy_data.get("rules", {})
                if isinstance(rules, dict):
                    result.policy_rules = rules
        except (ToolFailure, Exception):
            pass

        # 2. Thu thập thông tin từ các agent trước
        max_prior_strength = 0.0
        strongest_prior_issue: str | None = None
        all_prior_refs = list(result.evidence_refs)

        for prior_res in ctx.prior.values():
            all_prior_refs.extend(prior_res.evidence_refs)
            for issue, strength in prior_res.issue_signals.items():
                if issue in ISSUE_CODES and strength > max_prior_strength:
                    max_prior_strength = strength
                    strongest_prior_issue = issue

        # 3. Quyết định issue nếu không có lỗi nào từ hệ thống/vận chuyển
        issue_details: dict[str, IssueDetail] = {}

        if max_prior_strength < 0.20:
            if len(all_prior_refs) > 1:
                # Có dữ liệu kiểm tra từ các agent trước nhưng không có lỗi được xác nhận.
                detected_issue = "unsupported_claim"
                result.issue_signals["unsupported_claim"] = 0.85
                rc = ["CUSTOMER_MISUNDERSTANDING"]
                resp = [{"party_type": "customer", "party_id": None}]
                acts = ["document_no_action"]
                result.root_causes.extend(rc)
                result.responsible_parties.extend(resp)
                result.actions.extend(acts)
                issue_details["unsupported_claim"] = IssueDetail(
                    root_causes=rc,
                    responsible_parties=resp,
                    refund_lines=[],
                    actions=acts,
                )
            else:
                detected_issue = "insufficient_evidence"
                result.issue_signals["insufficient_evidence"] = 0.3
                rc = ["INSUFFICIENT_EVIDENCE"]
                resp = [{"party_type": "unknown", "party_id": None}]
                acts = ["request_customer_info"]
                result.root_causes.extend(rc)
                result.responsible_parties.extend(resp)
                result.actions.extend(acts)
                issue_details["insufficient_evidence"] = IssueDetail(
                    root_causes=rc,
                    responsible_parties=resp,
                    refund_lines=[],
                    actions=acts,
                )
        else:
            detected_issue = strongest_prior_issue or "insufficient_evidence"

        # 4. Đánh giá từng Claim của khách hàng (claim_assessments)
        customer_request = ctx.case.get("customer_request", {})
        claims_input = customer_request.get("claims", [])
        claim_assessments = []
        unique_prior_refs = list(dict.fromkeys(all_prior_refs))[:20]

        for claim in claims_input:
            claim_id = claim.get("claim_id")
            topic = claim.get("topic")
            if not claim_id:
                continue

            if topic == detected_issue:
                verdict = "supported"
                confidence = 0.90
            elif topic in ("requested_full_refund", "requested_refund", "requested_partial_refund"):
                if detected_issue in REFUND_JUSTIFYING_ISSUES:
                    verdict = "supported"
                    confidence = 0.90
                elif detected_issue in NO_ACTION_ISSUES or detected_issue == "refund_pending":
                    verdict = "unsupported"
                    confidence = 0.90
                elif detected_issue in ("late_delivery_seller", "late_delivery_logistics"):
                    verdict = "partially_supported"
                    confidence = 0.85
                else:
                    verdict = "unsupported"
                    confidence = 0.80
            elif topic in ISSUE_CODES:
                verdict = "unsupported"
                confidence = 0.88
            else:
                verdict = "unsupported"
                confidence = 0.80

            claim_assessments.append(
                {
                    "claim_id": str(claim_id)[:64],
                    "verdict": verdict,
                    "confidence": confidence,
                    "evidence_refs": unique_prior_refs,
                }
            )

        result.claims = claim_assessments
        result.issue_details = issue_details
        result.notes["policy_version"] = policy_ver
        result.notes["rules_loaded"] = len(result.policy_rules)
        return result


def verify(output: dict[str, Any], ctx: CaseContext) -> list[str]:
    """Kiểm tra invariants và tự động chuẩn hóa/sửa lỗi trước khi xuất output.

    Trả về danh sách các vấn đề phát hiện (rỗng nếu mọi thứ hoàn hảo).
    """
    issues: list[str] = []

    assessment = output.get("assessment", {})
    primary_issue = assessment.get("primary_issue")
    case_status = assessment.get("case_status")
    fin = output.get("financial_resolution", {})
    recommended_refund = fin.get("recommended_refund_brl", 0.0)
    refund_lines = fin.get("refund_lines", [])
    actions = output.get("resolution_actions", [])
    root_cause = output.get("root_cause_analysis", {})
    responsible_parties = root_cause.get("responsible_parties", [])

    # Tổng tiền các dòng hoàn phải khớp với số tiền được đề xuất.
    lines_sum = round(sum(float(line.get("amount_brl", 0.0)) for line in refund_lines), 2)
    if abs(recommended_refund - lines_sum) > 0.001:
        fin["recommended_refund_brl"] = lines_sum

    # 2. Status & Refund & Action Consistency
    if case_status == "no_action":
        if fin.get("recommended_refund_brl", 0.0) > 0:
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []
        actions = [a for a in actions if "refund" not in a.lower()]
        if not actions:
            actions = ["document_no_action"]
        output["resolution_actions"] = actions
    elif case_status == "action_required":
        if fin.get("recommended_refund_brl", 0.0) > 0 and not any(
            "refund" in a.lower() for a in actions
        ):
            actions.append("issue_refund")
        if not actions:
            actions.append("investigate_case")
        output["resolution_actions"] = actions

    # 3. Responsibility Consistency
    party_types = {p.get("party_type") for p in responsible_parties if isinstance(p, dict)}
    seller_ids = output.get("affected_entities", {}).get("seller_ids", [])
    first_seller = seller_ids[0] if seller_ids else None

    if primary_issue == "late_delivery_seller" and "seller" not in party_types:
        responsible_parties.append({"party_type": "seller", "party_id": first_seller})
    elif primary_issue == "late_delivery_logistics" and "logistics_provider" not in party_types:
        responsible_parties.append({"party_type": "logistics_provider", "party_id": None})
    elif primary_issue in ("canceled_order_paid", "unavailable_order_paid") and not party_types:
        responsible_parties.append({"party_type": "seller", "party_id": first_seller})
    elif (
        primary_issue in ("valid_split_payment", "unsupported_claim")
        and "customer" not in party_types
    ):
        responsible_parties.append({"party_type": "customer", "party_id": None})
    elif (
        primary_issue in ("duplicate_charge", "payment_mismatch", "refund_failed")
        and "payment_provider" not in party_types
    ):
        responsible_parties.append({"party_type": "payment_provider", "party_id": None})

    # Lọc unique và giới hạn số lượng responsible_parties (max 5)
    unique_parties = []
    seen_parties = set()
    for p in responsible_parties:
        if not isinstance(p, dict):
            continue
        ptype = p.get("party_type")
        pid = p.get("party_id")
        if ptype in PARTY_TYPES:
            key = (ptype, str(pid) if pid is not None else None)
            if key not in seen_parties:
                seen_parties.add(key)
                unique_parties.append({"party_type": ptype, "party_id": pid})
    root_cause["responsible_parties"] = unique_parties[:5]

    # 4. Action uniqueness & length
    clean_actions = []
    seen_act = set()
    for act in output.get("resolution_actions", []):
        act_str = str(act).strip()[:80]
        if act_str and act_str not in seen_act:
            seen_act.add(act_str)
            clean_actions.append(act_str)
    output["resolution_actions"] = clean_actions[:8]

    # 5. Check Evidence Refs
    ev_refs = output.get("evidence_refs", [])
    if not ev_refs and primary_issue != "insufficient_evidence":
        issues.append("missing_evidence_refs")

    return issues
