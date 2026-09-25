# L3A Architecture Record

Hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử K4 L3A tuân thủ các Public Contracts, Scoring Policy và MCP Audit.

## 1. System overview

Quy trình tuần tự theo từng case qua các specialist. `run_case()` tạo ledger và cache riêng cho mỗi case, gọi các agent theo thứ tự, chọn `primary_issue` từ `issue_signals`, áp dụng rule của Policy Agent, kiểm tra schema và gọi verifier trước khi trả output.

```text
Input → Coordinator → Order/Item Agent → Shipment/Seller Agent → Payment/Refund Agent → Policy Agent → Verifier Agent → Output
                             │                     │                     │                  │               │
                             └─────────────────────┴─────── MCP ─────────┴──────────────────┘               │
                                                                   │                                        │
                                                                   └─────────────────── Trace ──────────────┘
```

## 2. Agent ownership

| Actor | Allowed Tools | Input | Trách nhiệm | Output / Handoff |
| --- | --- | --- | --- | --- |
| **Coordinator** | *(None directly)* | `case` từ `inputs/` | Tạo ledger/cache theo case, giao việc, chọn issue, áp policy, tổng hợp findings, kiểm tra schema, phát `task_assigned` và `handoff`. CLI phát `case_received` và `case_finalized`. | Chuyển giao `CaseContext` cho từng specialist agent. |
| **Order/Item Agent** | `get_order`, `get_order_items`, `get_product_context` | `claimed_order_id`, `case_id` | Truy vấn chi tiết đơn hàng, danh mục mặt hàng, phát hiện đơn bị hủy (`canceled_order_paid`) hoặc hết hàng (`unavailable_order_paid`). Điền entities `order_ids`, `item_ids`, `seller_ids`. | `SpecialistResult` (issue_signals, entities, root_causes, actions). |
| **Shipment/Seller Agent** | `get_shipment_summary`, `get_sellers` | `claimed_order_id`, `prior` sellers | Truy vấn hành trình vận chuyển, so sánh mốc cam kết vs thực tế để phân biệt trễ do người bán (`late_delivery_seller`) hay do đối tác vận chuyển (`late_delivery_logistics`). Điền entity `shipment_ids`. | `SpecialistResult` (responsible_parties, root_causes, actions). |
| **Payment/Refund Agent** | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `claimed_order_id`, `order_status` | Đối soát dòng tiền: phát hiện trùng thanh toán (`duplicate_charge`), sai lệch tiền (`payment_mismatch`), chia tiền hợp lệ (`valid_split_payment`), lỗi hoàn tiền (`refund_failed`) hay đang xử lý (`refund_pending`). Tính `refund_lines`. | `SpecialistResult` (payment_references, refund_lines, actions). |
| **Policy Agent** | `get_policy`, `get_customer_history` | `policy_version`, `customer_request.claims`, `prior` findings | Lấy rule qua MCP, đối chiếu khiếu nại với findings, đánh giá claim. Coordinator chọn `primary_issue` và phát `policy_decided` khi áp rule. | `SpecialistResult` (claim_assessments, policy_rules). |
| **Verifier** | *(None directly)* | Draft `output`, `CaseContext` | Chuẩn hóa tổng tiền, trạng thái, hành động và bên chịu trách nhiệm; coordinator kiểm tra lại schema và phát `verification_completed`. | Output đã kiểm tra hoặc fallback an toàn. |

## 3. A2A protocol

- **Message Envelope:** Các agent trao đổi thông qua `CaseContext`:
  - `case`: Dữ liệu gốc của case (read-only).
  - `gateway`: `ScopedGateway` chỉ cho phép gọi các tool được cấp quyền.
  - `trace`: `TraceWriter` ghi nhận observable events.
  - `prior`: Dict chứa `SpecialistResult` của tất cả các agent đã chạy trước đó trong cùng case.
- **Correlation:** Tất cả tool calls và trace events đều được gán `case_id` tương ứng, ngăn chặn tuyệt đối rò rỉ dữ liệu chéo case.
- **Handoff:** Coordinator phát event `task_assigned` khi giao việc và nhận lại `handoff` (`findings_ready` hoặc `specialist_failed`).

## 4. Evidence lifecycle

- **Validation:** Mọi phản hồi từ MCP Server đều được kiểm tra hợp lệ với `mcp-evidence-response-v1.schema.json`.
- **Per-case Ledger:** `ScopedGateway` tự động ghi nhận từng `evidence_ref` được server trả về vào một `ledger` riêng của case đó.
- **Provenance Gate:** Hàm `assemble()` chỉ cho phép các `evidence_refs` tồn tại trong `ledger` được đưa vào output cuối cùng. Tuyệt đối không sinh mã giả.
- **Trace Consumption:** Ngay khi tool trả về kết quả, `ScopedGateway` tự động phát sự kiện trace `tool_result_consumed` với `actor`, `tool_name` và `evidence_refs`.
- **Tool efficiency:** Cache theo case và bộ tham số dùng chung giữa các agent. Shipment chỉ gọi `get_sellers` khi chưa có seller ID từ order hoặc shipment. Mỗi agent chỉ được gọi các tool khai báo trong `tools`.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event / code |
| --- | --- | --- | --- |
| **MCP timeout** | Có (tối đa 2 lần, exponential backoff: 0.5s, 1.0s) | Báo `ToolFailure("timeout")`, specialist bỏ qua tool và dùng dữ liệu tối thiểu. | `handoff` với `decision_code="specialist_failed"` |
| **Transport error** | Có (tối đa 2 lần) | Báo `ToolFailure("transport")`. | `handoff` với `decision_code="specialist_failed"` |
| **Tool error (Server)** | Không retry | Không đoán dữ liệu, ghi nhận lỗi. | `handoff` với `decision_code="specialist_failed"` |
| **Source conflict** | Không retry | Ưu tiên dữ liệu MCP so với lời kể của khách; ghi `data_conflicts` với nguồn được chọn. | `tool_result_consumed`, `verification_completed` |
| **Invalid specialist result** | Không | Coordinator dùng fallback an toàn `_safe_output()`. | `verification_completed` với `schema_fallback` |

## 6. Verification invariants

Trước khi xuất file output, `Verifier Agent` kiểm tra và chuẩn hóa các quy tắc:
1. **Financial Invariant:** Tổng `amount_brl` trong `refund_lines` phải khớp chính xác với `recommended_refund_brl`.
2. **Status ↔ Refund Consistency:** Nếu `case_status == "no_action"`, thì `recommended_refund_brl` bắt buộc bằng `0.0` và `refund_lines` rỗng.
3. **Action Consistency:**
   - Nếu `recommended_refund_brl > 0`, `resolution_actions` bắt buộc phải có `"issue_refund"`.
   - Nếu `case_status == "no_action"`, `resolution_actions` tuyệt đối không chứa bất kỳ hành động nào liên quan đến refund.
4. **Responsibility Invariant:**
   - Khi `late_delivery_seller`: trong `responsible_parties` phải có `party_type == "seller"`.
   - Khi `late_delivery_logistics`: trong `responsible_parties` phải có `party_type == "logistics_provider"`.
5. **Entity Scope:** Mọi entity IDs (`order_ids`, `item_ids`,...) phải có nguồn gốc từ dữ liệu thực tế quan sát được.
6. **Confidence Calibration:** `decide()` kết hợp tín hiệu mạnh nhất với tỷ trọng của nó trong các tín hiệu cạnh tranh; giới hạn tối đa 0.95. Khi không có evidence, confidence tối đa 0.2. Đây là heuristic, chưa được hiệu chỉnh trên nhãn ẩn.

## 7. Reproducibility

- **Môi trường:** Python >= 3.11, chạy trên Windows/Linux.
- **Dependencies:** `httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `mcp>=2,<3`, `python-dotenv>=1.1,<2`.
- **Cơ chế ra quyết định:** Deterministic rule-based, đảm bảo kết quả 100% tái lập, thời gian thực thi nhanh và không phụ thuộc chi phí/độ trễ của API bên thứ ba.
- **A2A và retry:** Mỗi agent chạy một lần theo thứ tự; MCP timeout 30 giây, thử lại tối đa 2 lần với chờ 0.5 và 1 giây cho lỗi tạm thời. Issue hòa điểm được phá theo thứ tự `ISSUE_CODES`.
- **Song song giữa các case:** CLI xử lý tối đa 8 case đồng thời; ledger, cache và kết quả của mỗi case độc lập. Trong từng case, các agent vẫn chạy tuần tự để dùng `ctx.prior`.
- **Lệnh thực thi:**
  - Chạy toàn bộ 100 cases: `day09 run`
  - Kiểm tra tính hợp lệ: `day09 validate`
  - Đóng gói submission: `day09 package --output dist/submission.zip`
