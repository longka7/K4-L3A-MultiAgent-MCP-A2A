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
    ScopedGateway,
    Specialist,
    SpecialistResult,
)
from .contracts import ContractError
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CAUSE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
# Assumption to re-check against the public score: these two issues need no customer action.
NO_ACTION_ISSUES = frozenset({"valid_split_payment", "unsupported_claim"})
MIN_SIGNAL = 0.05

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


def decide(results: Sequence[SpecialistResult]) -> tuple[str, str, float]:
    """Pick primary_issue, case_status and confidence from specialist signals."""
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
    if top_code == "insufficient_evidence":
        status = "needs_investigation"
    elif top_code in NO_ACTION_ISSUES:
        status = "no_action"
    else:
        status = "action_required"
    return top_code, status, confidence


def assemble(
    case_id: str, results: Sequence[SpecialistResult], ledger: dict[str, str]
) -> dict[str, Any]:
    """Build the schema-shaped output. Only evidence_refs seen via MCP survive."""
    issue, status, confidence = decide(results)

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
    causes = [
        c for c in _unique([c for r in results for c in r.root_causes]) if CAUSE_PATTERN.match(c)
    ]
    parties = _unique(
        [
            {"party_type": p["party_type"], "party_id": p.get("party_id")}
            for r in results
            for p in r.responsible_parties
            if p.get("party_type") in PARTY_TYPES
        ]
    )
    refund_lines = [
        {
            "reason_code": str(line["reason_code"])[:80],
            "amount_brl": round(max(float(line["amount_brl"]), 0.0), 2),
            "entity_id": line.get("entity_id"),
        }
        for r in results
        for line in r.refund_lines
    ][:10]
    evidence = real(
        [ref for r in results for ref in r.evidence_refs]
        + [ref for c in claims for ref in c["evidence_refs"]]
    )[:30]

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
            "responsible_parties": parties[:5],
        },
        "evidence_refs": evidence,
        "data_conflicts": _unique([c for r in results for c in r.conflicts])[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": round(sum(line["amount_brl"] for line in refund_lines), 2),
            "refund_lines": refund_lines,
        },
        "resolution_actions": _unique([a[:80] for r in results for a in r.actions if a.strip()])[
            :8
        ],
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

    output = assemble(case_id, list(results.values()), ledger)
    try:
        trace.contracts.validate_output(output, f"outputs/{case_id}.json")
        fallback = False
    except ContractError:
        output, fallback = _safe_output(case_id, ledger), True

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
