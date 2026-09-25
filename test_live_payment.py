import asyncio
import json
from pathlib import Path

from student_agent.agents.contract import CaseContext, ScopedGateway
from student_agent.agents.payment_refund import PaymentRefundAgent
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter


async def run_live_test(case_filename: str):
    root = Path(__file__).resolve().parent
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")

    case_path = root / "inputs" / case_filename
    if not case_path.exists():
        print(f"File {case_path} không tồn tại!")
        return

    case = json.loads(case_path.read_text(encoding="utf-8"))
    case_id = case["case_id"]
    print(f"--- Đang chạy thử nghiệm với Case: {case_id} ---")
    print(f"Customer claimed order: {case.get('customer_request', {}).get('claimed_order_id')}")
    print(f"Claims: {case.get('customer_request', {}).get('claims')}\n")

    trace_path = root / "traces" / "test_live_trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    ledger: dict[str, str] = {}
    cache: dict = {}

    print(f"1. Đang kết nối tới MCP Gateway tại: {settings.mcp_endpoint} ...")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        tools = await gateway.list_tools()
        print(f"   Kết nối thành công! Danh sách tools khả dụng: {tools}\n")

        scoped = ScopedGateway(
            gateway,
            case_id=case_id,
            actor="payment-refund-agent",
            allowed_tools=PaymentRefundAgent.tools,
            trace=trace,
            ledger=ledger,
            cache=cache,
        )

        ctx = CaseContext(case=case, gateway=scoped, trace=trace)
        agent = PaymentRefundAgent()

        print("2. PaymentRefundAgent bắt đầu điều tra...")
        try:
            raw_payments = await gateway.call("get_order_payments", case_id=case_id, order_id=case.get("customer_request", {}).get("claimed_order_id"))
            print(f"   [Debug MCP get_order_payments data]: {raw_payments.get('data')}")
            raw_pt = await gateway.call("get_payment_timeline", case_id=case_id, order_id=case.get("customer_request", {}).get("claimed_order_id"))
            print(f"   [Debug MCP get_payment_timeline data]: {raw_pt.get('data')}")
            raw_rt = await gateway.call("get_refund_timeline", case_id=case_id, order_id=case.get("customer_request", {}).get("claimed_order_id"))
            print(f"   [Debug MCP get_refund_timeline data]: {raw_rt.get('data')}")
            raw_order = await gateway.call("get_order", case_id=case_id, order_id=case.get("customer_request", {}).get("claimed_order_id"))
            print(f"   [Debug MCP get_order data]: {raw_order.get('data')}")
        except Exception as e:
            print(f"   [Debug MCP call failed]: {e}")

        result = await agent.run(ctx)

        print("\n=== KẾT QUẢ TỪ MCP & PAYMENT AGENT ===")
        print(f"- Issue Signals: {result.issue_signals}")
        print(f"- Payment References bóc tách được: {result.entities.get('payment_references', [])}")
        print(f"- Root Causes: {result.root_causes}")
        print(f"- Responsible Parties: {result.responsible_parties}")
        print(f"- Refund Lines đề xuất: {result.refund_lines}")
        print(f"- Evidence Refs hợp lệ thu được: {result.evidence_refs}")
        print(f"- Ghi chú dữ liệu: {result.notes}")


if __name__ == "__main__":
    # Test thử với case L3A_CASE_005 (liên quan đến payment) hoặc L3A_CASE_001
    asyncio.run(run_live_test("L3A_CASE_005.json"))
