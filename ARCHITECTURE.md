# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng chạy tuần tự trong một tiến trình, mỗi case độc lập (ledger, cache và trace theo `case_id`):

```text
inputs/<case_id>.json
  → case_received (day09 run)
  → Coordinator ── task_assigned ──► Specialist 1..N (order-item, shipment-seller,
  │                                   payment-refund, policy), lần lượt, mỗi agent chạy một lần
  │        ◄── handoff(findings_ready) + SpecialistResult
  → decide(): chọn primary_issue từ issue_signals mạnh nhất
  → áp dụng get_policy (nếu có) → assemble() → validate schema
  → policy_decided → handoff(verify_request) → Verifier → verification_completed
  → outputs/<case_id>.json → case_finalized (day09 run)
```

Mọi lời gọi MCP đi qua `ScopedGateway`, mọi sự kiện đi qua `TraceWriter`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case` (case_id, customer_request, policy_version), kết quả các specialist | Giao việc, chọn `primary_issue`, áp policy, ghép output đúng schema, phát trace vòng đời; không gọi tool MCP nào | `outputs/<case_id>.json`, trace, handoff sang verifier |
| Order/item | TODO | TODO | TODO |
| Payment | TODO | TODO | TODO |
| Shipment | TODO | TODO | TODO |
| Policy | TODO | TODO | TODO |
| Verifier | TODO | TODO | TODO |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

- **Envelope:** `SpecialistResult` (`agents/contract.py`), trao đổi bằng lời gọi hàm trong tiến trình.
  Không có message bất đồng bộ nên không có vòng lặp: pipeline tuyến tính, mỗi agent chạy đúng một lần.
- **Correlation:** `case_id` có trong mọi lệnh gọi tool (do `ScopedGateway` tự chèn) và mọi trace event.
- **Handoff:** coordinator giao việc (`task_assigned`) rồi nhận kết quả (`handoff` với `findings_ready`
  hoặc `specialist_failed`); agent chạy sau đọc kết quả agent trước qua `ctx.prior`. Cuối cùng
  coordinator chuyển output cho verifier (`handoff` với `verify_request`).
- **Timeout:** mỗi lời gọi MCP tối đa 30 giây; hết hạn thì retry theo failure policy bên dưới.
- **Trace:** chỉ ghi sự kiện và mã quyết định quan sát được, không ghi prompt hay suy luận riêng.

## 4. Evidence lifecycle

1. Server trả evidence; `EvidenceGateway` validate theo `mcp-evidence-response-v1`.
2. `ScopedGateway` ghi `evidence_ref` vào ledger của đúng case và phát `tool_result_consumed`
   (một event cho mỗi cặp actor + evidence, kể cả khi trúng cache, để trace không phình).
3. Agent đưa ref vào `SpecialistResult.evidence_refs` hoặc `claims`.
4. `assemble()` chỉ giữ ref có trong ledger. Ref bịa hoặc ref của case khác không thể lọt vào output.
5. Ledger và cache được tạo trong `run_case`, nên evidence không bao giờ dùng lại giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có: tối đa 2 lần, chờ 0,5 s rồi 1 s (các tool đều chỉ đọc nên an toàn khi gọi lại) | `ToolFailure(kind=timeout)`; agent quyết định, nếu agent lỗi thì coordinator bỏ qua agent đó và đi tiếp | `handoff` / `specialist_failed` + `error_type` |
| Not found | Không (server báo lỗi rõ ràng) | `ToolFailure(kind=tool_error)`; agent không phát tín hiệu → có thể ra `insufficient_evidence`, không đoán dữ liệu | `handoff` / `specialist_failed` hoặc note của agent |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | Không | Output sai schema thì dùng output an toàn (`insufficient_evidence`, không claim, chỉ evidence từ MCP) thay vì làm hỏng cả lượt chạy | `verification_completed` / `issues_found` chứa `schema_fallback` |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Coordinator tự bảo đảm trong `assemble()`:
- `case_id` của output bằng `case_id` của input; output khớp JSON Schema (nếu không thì dùng fallback).
- Mọi `evidence_refs` (kể cả trong claim) đều nằm trong ledger MCP của đúng case.
- Chỉ giữ hành động, bên chịu trách nhiệm, dòng hoàn tiền và root cause của issue thắng cuộc.
- Khi có `get_policy`: `case_status`, `resolution_actions` và loại bên chịu trách nhiệm theo rule của issue;
  refund bằng 0 theo policy thì bỏ `refund_lines`.
- `recommended_refund_brl` bằng tổng `refund_lines`; `confidence` tối đa 0,2 khi không có evidence.
- Mỗi claim trong input đều có một `claim_assessment`.

Verifier bổ sung (Người 5): tổng tiền, status↔refund↔action, seller responsibility, xung đột dữ liệu.
Cờ `no_evidence`, `refund_on_no_action`, `schema_fallback` được ghi vào `verification_completed`.

## 7. Reproducibility

- Không dùng LLM và không có yếu tố ngẫu nhiên; hòa điểm giữa các issue được phá theo thứ tự cố định
  của `ISSUE_CODES`, nên cùng dữ liệu luôn cho cùng output.
- Chạy tuần tự (concurrency = 1), mỗi lời gọi MCP timeout 30 s, retry tối đa 2 lần.
- Python >= 3.11, `mcp` 2.x (`mcp_gateway.py` đọc cả `is_error` và `isError`).
- Lệnh: `python -m pip install -e ".[dev]"`, `day09 run`, `day09 validate`, `day09 package`.
- Giới hạn: mỗi file trong ZIP tối đa 1 MB. Trace hiện khoảng 320 KB cho 100 case (agent rỗng); cần theo dõi
  khi các agent gọi nhiều tool.
- Không ghi API key vào repo, output hay trace.
