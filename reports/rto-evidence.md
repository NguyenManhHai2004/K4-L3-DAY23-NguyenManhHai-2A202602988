# RTO/RPO Evidence — Lab 23

Mọi con số dưới đây truy được về một dòng log thật (`đường/dẫn:số_dòng`). Các số được
`tools/measure_rto.py` tính từ timestamp; kết quả đầy đủ lưu ở `reports/measure-drill-1.json`
và `reports/measure-drill-2.json`. Thời gian ISO trong `chaos-events` là UTC (log failover/runbook
dùng giờ local = UTC+7).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:14:18Z` | chaos kill (netblock Region A) | `chaos/chaos-events.jsonl:2` |
| Request fail đầu tiên | `+2.1s` | dòng `ok:false` đầu tiên sau t_outage (ReadTimeout) | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage; 14 request fail | `reports/measure-drill-1.json:28` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json:25` |

(`chaos/chaos-events.jsonl:1` là lần thử đầu bị lỗi PID trên Windows — không có process nào bị dừng, nên không dùng làm mốc.)

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` lúc 04:32:01Z | `chaos/chaos-events.jsonl:6` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +14.9s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +23.1s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +29.9s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +29.9s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+31.6s** | dòng `ok:true` đầu sau lỗi (phục vụ bởi Region B) | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `31.6s` | 300s (5 phút) | PASS (nhanh hơn mục tiêu 268.4s) |
| RPO — Vector DB | `16.01s` / `8` doc | 300s (5 phút) | PASS (`reports/failover-events.jsonl:2`) |

Ghi chú: `reports/health-events.jsonl:1` là Region B báo UNHEALTHY *trước* khi kill — đúng, vì B
cố ý rỗng/warm nên `/readyz` trả 503 (process sống ≠ có thể serve). Nó không ảnh hưởng RTO.

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 14.9s (floor 15s = 5s × 3) | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm interval (vd 2s × 3 = 6s) nhưng đánh đổi rủi ro flapping |
| Chờ runbook bắt đầu failover | 8.1s (từ +14.9s đến +23.0s) | `reports/runbook-run.jsonl:1` — ba lần probe tuần tự, mỗi lần timeout 2s | Bỏ re-probe khi đã có alert của health checker, hoặc probe song song |
| Snapshot restore | 0.02s | 2_restore → 3_scale, `reports/failover-events.jsonl:2` | Đã gần 0 ở lab (copy file cục bộ); với S3 thật phụ thuộc kích thước model |
| GPU pool warm-up | 6.85s | `waited_s` ở `reports/failover-events.jsonl:4` | Giữ pool ở trạng thái warm lớn hơn / pre-warm GPU |
| DNS/LB TTL cache | 1.6s | t_recovered − t_cutover (`reports/drill-2-withdr.jsonl:39` so với `reports/failover-events.jsonl:5`) | Giảm `EDGE_TTL_SECONDS` (hiện 5s) |
