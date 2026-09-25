from __future__ import annotations

import asyncio
import json
from pathlib import Path

from student_agent.agents.contract import CaseContext, ScopedGateway
from student_agent.agents.order_item import OrderItemAgent
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter


async def main() -> None:
    root = Path(__file__).resolve().parent
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")

    case_path = root / "inputs" / "L3A_CASE_001.json"
    if not case_path.exists():
        print(f"Không tìm thấy file: {case_path}")
        return

    case = json.loads(case_path.read_text(encoding="utf-8"))
    trace_path = root / "traces" / "test_trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    agent = OrderItemAgent()
    print(f"Đang kết nối tới MCP Gateway: {settings.mcp_endpoint} ...")

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        tools = await gateway.list_tools()
        print(f"MCP Gateway sẵn sàng, các tool khả dụng: {tools}")

        scoped = ScopedGateway(
            gateway,
            case_id=case["case_id"],
            actor=agent.name,
            allowed_tools=agent.tools,
            trace=trace,
            ledger={},
            cache={},
        )
        ctx = CaseContext(case, scoped, trace, {})
        print(f"Đang chạy OrderItemAgent trên case: {case['case_id']} (order_id={ctx.claimed_order_id}) ...")

        res = await agent.run(ctx)

        print("\n================= KẾT QUẢ TỪ MCP THẬT =================")
        print(f"Actor: {res.agent}")
        print(f"Tín hiệu lỗi (issue_signals): {res.issue_signals}")
        print(f"Entities thu thập được: {res.entities}")
        print(f"Evidence refs (thật từ server): {res.evidence_refs}")
        print(f"Đánh giá Claims: {res.claims}")
        print(f"Nguyên nhân gốc (root_causes): {res.root_causes}")
        print(f"Dòng hoàn tiền (refund_lines): {res.refund_lines}")
        print(f"Dữ liệu trung gian (notes): {res.notes}")
        print("========================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
