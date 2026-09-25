from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .agents import SPECIALISTS, verify
from .agents.contract import (
    ENTITY_KEYS,
    ISSUE_CODES,
    PARTY_TYPES,
    CaseContext,
    IssueDetail,
    ScopedGateway,
    Specialist,
    SpecialistResult,
)
from .contracts import ContractError
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CAUSE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
CASE_STATUSES = frozenset({"action_required", "no_action", "needs_investigation"})
# Defaults used only when get_policy evidence is missing. EC_POLICY_V1 confirms both sets.
NO_ACTION_ISSUES = frozenset({"valid_split_payment", "unsupported_claim"})
NEEDS_INVESTIGATION_ISSUES = frozenset({"insufficient_evidence", "refund_pending"})
MIN_SIGNAL = 0.05
NO_EVIDENCE_CONFIDENCE_CAP = 0.2

Verifier = Callable[[dict[str, Any], CaseContext], list[str]]


def _unique(items: Sequence[Any]) -> list[Any]:
    seen: set[Any] = set()
    result = []
    for item in items:
        key = item if isinstance(item, str) else repr(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def default_status(issue: str) -> str:
    if issue in NO_ACTION_ISSUES:
        return "no_action"
    if issue in NEEDS_INVESTIGATION_ISSUES:
        return "needs_investigation"
    return "action_required"


def decide(results: Sequence[SpecialistResult]) -> tuple[str, str, float]:
    """Pick primary_issue, a default case_status and confidence from specialist signals."""
    strongest: dict[str, float] = {}
    for result in results:
        for code, strength in result.issue_signals.items():
            if code in ISSUE_CODES:
                strongest[code] = max(strongest.get(code, 0.0), min(max(strength, 0.0), 1.0))
    positive = {code: s for code, s in strongest.items() if s >= MIN_SIGNAL}
    if not positive:
        return "insufficient_evidence", "needs_investigation", 0.2

    top_code = min(positive, key=lambda code: (-positive[code], ISSUE_CODES.index(code)))
    top = positive[top_code]
    share = top / sum(positive.values())
    # Placeholder calibration: blend absolute strength with margin over competing issues.
    confidence = round(min(max(0.5 * top + 0.5 * share, 0.05), 0.95), 3)
    return top_code, default_status(top_code), confidence


def policy_rule_for(results: Sequence[SpecialistResult], issue: str) -> dict[str, Any] | None:
    """The get_policy rule for ``issue``, if the policy agent supplied one."""
    for result in results:
        rule = result.policy_rules.get(issue)
        if isinstance(rule, dict):
            return rule
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merged_details(results: Sequence[SpecialistResult], issue: str) -> IssueDetail:
    merged = IssueDetail()
    for result in results:
        detail = result.details_for(issue)
        merged.root_causes += detail.root_causes
        merged.responsible_parties += detail.responsible_parties
        merged.refund_lines += detail.refund_lines
        merged.actions += detail.actions
    return merged


def _parties(candidates: Sequence[dict[str, Any]], rule: dict[str, Any] | None) -> list[dict]:
    valid = [
        {
            "party_type": p["party_type"],
            "party_id": None if p.get("party_id") is None else str(p["party_id"]),
        }
        for p in candidates
        if p.get("party_type") in PARTY_TYPES
    ]
    if not rule:
        return _unique(valid)
    rule_types = _unique(
        [
            p["party_type"]
            for p in rule.get("responsible_parties", [])
            if isinstance(p, dict) and p.get("party_type") in PARTY_TYPES
        ]
    )
    kept = [p for p in valid if p["party_type"] in rule_types]
    if kept:
        return _unique(kept)
    # The policy's party_id is a fixed sample shared by every case, so only its type is trusted.
    return [{"party_type": party_type, "party_id": None} for party_type in rule_types]


def _claim_fallbacks(
    case: dict[str, Any],
    issue: str,
    status: str,
    confidence: float,
    evidence: list[str],
    assessed: set[str],
) -> list[dict[str, Any]]:
    """One assessment per input claim that no specialist assessed (derived, not invented)."""
    claims = []
    for claim in case.get("customer_request", {}).get("claims", []):
        claim_id, topic = claim.get("claim_id"), claim.get("topic")
        if not isinstance(claim_id, str) or claim_id in assessed:
            continue
        if topic == "requested_full_refund":
            verdict = {"no_action": "unsupported", "needs_investigation": "insufficient_evidence"}
            verdict = verdict.get(status, "partially_supported")
        elif issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == issue and topic != "unsupported_claim":
            verdict = "supported"
        else:
            verdict = "unsupported"
        claims.append(
            {
                "claim_id": claim_id[:64],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence[:30],
            }
        )
    return claims


def assemble(
    case: dict[str, Any], results: Sequence[SpecialistResult], ledger: dict[str, str]
) -> dict[str, Any]:
    """Build the schema-shaped output for the winning issue.

    Only evidence_refs seen via MCP survive, and only the winning issue's details are kept.
    """
    case_id = case["case_id"]
    issue, status, confidence = decide(results)
    rule = policy_rule_for(results, issue)
    if rule and rule.get("case_status") in CASE_STATUSES:
        status = rule["case_status"]
    detail = _merged_details(results, issue)

    def real(refs: Sequence[str]) -> list[str]:
        return [ref for ref in _unique(list(refs)) if ref in ledger]

    entities = {
        key: sorted({str(v) for r in results for v in r.entities.get(key, [])})[:20]
        for key in ENTITY_KEYS
    }
    claims = []
    for result in results:
        for claim in result.claims:
            claims.append({**claim, "evidence_refs": real(claim.get("evidence_refs", []))[:30]})
    evidence = real(
        [ref for r in results for ref in r.evidence_refs]
        + [ref for c in claims for ref in c["evidence_refs"]]
    )[:30]
    if not evidence:
        confidence = min(confidence, NO_EVIDENCE_CONFIDENCE_CAP)
    claims += _claim_fallbacks(
        case, issue, status, confidence, evidence, {c["claim_id"] for c in claims}
    )

    causes = [c for c in _unique(detail.root_causes) if CAUSE_PATTERN.match(c)]
    refund_lines = [
        {
            "reason_code": str(line["reason_code"])[:80],
            "amount_brl": round(max(float(line["amount_brl"]), 0.0), 2),
            "entity_id": line.get("entity_id"),
        }
        for line in detail.refund_lines
    ][:10]
    actions = [a[:80] for a in _unique(detail.actions) if a.strip()]
    if rule:
        if _number(rule.get("refund_brl")) == 0:
            refund_lines = []  # nothing to refund per policy: keep refund aligned with status
        action = rule.get("recommended_action")
        if isinstance(action, str) and action.strip():
            actions = [action.strip()[:80]]

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": entities,
        "claim_assessments": claims[:5],
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": code, "rank": rank} for rank, code in enumerate(causes[:5], 1)
            ],
            "responsible_parties": _parties(detail.responsible_parties, rule)[:5],
        },
        "evidence_refs": evidence,
        "data_conflicts": _unique([c for r in results for c in r.conflicts])[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": round(sum(line["amount_brl"] for line in refund_lines), 2),
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions[:8],
    }


def _safe_output(case_id: str, ledger: dict[str, str]) -> dict[str, Any]:
    """Schema-valid, claim-free fallback used only if a specialist produced malformed data."""
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {key: [] for key in ENTITY_KEYS},
        "claim_assessments": [],
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": sorted(ledger)[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def _builtin_checks(output: dict[str, Any]) -> list[str]:
    issues = []
    if not output["evidence_refs"]:
        issues.append("no_evidence")
    if output["assessment"]["case_status"] == "no_action" and (
        output["financial_resolution"]["recommended_refund_brl"] > 0
    ):
        issues.append("refund_on_no_action")
    return issues


async def run_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    specialists: Sequence[Specialist],
    verifier: Verifier,
) -> dict[str, Any]:
    case_id = case["case_id"]
    ledger: dict[str, str] = {}
    cache: dict[Any, dict[str, Any]] = {}

    def scoped(actor: str, tools: Any) -> ScopedGateway:
        return ScopedGateway(
            gateway,
            case_id=case_id,
            actor=actor,
            allowed_tools=tools,
            trace=trace,
            ledger=ledger,
            cache=cache,
        )

    results: dict[str, SpecialistResult] = {}
    for agent in specialists:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=agent.name,
            decision_code="investigate",
        )
        ctx = CaseContext(case, scoped(agent.name, agent.tools), trace, dict(results))
        try:
            result = await agent.run(ctx)
        except Exception as exc:  # one failing specialist must not abort the case
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=agent.name,
                target="coordinator",
                decision_code="specialist_failed",
                attributes={"error_type": type(exc).__name__},
            )
            continue
        results[agent.name] = result
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=agent.name,
            target="coordinator",
            decision_code="findings_ready",
            attributes={
                **dict(list(result.notes.items())[:18]),
                "evidence_count": len(result.evidence_refs),
            },
        )

    output = assemble(case, list(results.values()), ledger)
    try:
        trace.contracts.validate_output(output, f"outputs/{case_id}.json")
        fallback = False
    except ContractError:
        output, fallback = _safe_output(case_id, ledger), True

    issue = output["assessment"]["primary_issue"]
    if not fallback and policy_rule_for(list(results.values()), issue):
        actions = output["resolution_actions"]
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="coordinator",
            decision_code=issue,
            attributes={
                "case_status": output["assessment"]["case_status"],
                "action": actions[0] if actions else None,
            },
        )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="verify_request",
    )
    issues = _builtin_checks(output)
    if fallback:
        issues.append("schema_fallback")
    try:
        issues += verifier(output, CaseContext(case, scoped("verifier", ()), trace, results))
    except Exception:
        issues.append("verifier_error")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="issues_found" if issues else "passed",
        attributes={"issue_count": len(issues), "issues": ",".join(issues)[:150]},
    )
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3A coordinator: runs specialists in order, merges findings, verifies, returns output.

    Specialists live in ``student_agent/agents``; the starter kit intentionally never
    invents evidence, so an agent that finds nothing yields ``insufficient_evidence``.
    """
    return await run_case(case, gateway, trace, SPECIALISTS, verify)
