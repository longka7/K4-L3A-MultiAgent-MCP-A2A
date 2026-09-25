from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway

CONTRACTS = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
EVIDENCE = {
    "schema_version": "day09-mcp-evidence-v1",
    "evidence_ref": "ev_" + "a" * 24,
    "result_hash": "sha256:" + "0" * 64,
    "domain": "order",
    "data": {},
}


class FakeSession:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def call_tool(self, name: str, arguments: dict[str, str]) -> Any:
        return self.result


def call(result: Any) -> dict[str, Any]:
    gateway = EvidenceGateway(FakeSession(result), CONTRACTS)  # type: ignore[arg-type]
    return asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="o1"))


@pytest.mark.parametrize("error_field", ["is_error", "isError"])
def test_gateway_reads_evidence_with_either_mcp_field_style(error_field: str) -> None:
    result = SimpleNamespace(content=[], structured_content=EVIDENCE, **{error_field: False})
    assert call(result)["evidence_ref"] == EVIDENCE["evidence_ref"]


@pytest.mark.parametrize("error_field", ["is_error", "isError"])
def test_gateway_raises_on_tool_error_with_either_field_style(error_field: str) -> None:
    block = SimpleNamespace(text="not found")
    result = SimpleNamespace(content=[block], structured_content=None, **{error_field: True})
    with pytest.raises(RuntimeError, match="not found"):
        call(result)
