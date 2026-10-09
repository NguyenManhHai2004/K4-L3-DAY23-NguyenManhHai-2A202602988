# Runbook 1 trang — Region chính (A) down → failover sang Region B

Chạy từ thư mục gốc repo. Dùng `python` (Windows) hoặc `python3` (Linux/Mac). Runbook này
đã được chạy thật trong Drill 2: RTO đo được 31.6s (`reports/measure-drill-2.json`).

**Trước khi bắt đầu:** `python dr/health_checker.py --interval 5 --threshold 3 --duration 600 --out reports/health-events.jsonl`
đang chạy ở terminal khác, và `python state/replicate.py --every 30 --duration 600 --backend fs` đang cho ra snapshot mới.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage (đừng tin 1 lần fail) | `python chaos/kill_region.py status` (chạy 3 lần cách nhau 5s) | `a.alive=false` hoặc `a.ready=false` 3 lần liên tiếp, và `reports/health-events.jsonl` có `to:UNHEALTHY, region:a` | on-call |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` (hỏi confirm y/N; thêm `--auto` chỉ trong CI/chấm điểm) | dòng `step:2 thong_bao_incident` trong `reports/runbook-run.jsonl` có `t_known` | on-call |
| 3 | Restore state ở Region B | tự chạy bên trong runbook; chạy tay: `python state/snapshot.py get --region b --backend fs` | `reports/failover-events.jsonl` có `step:2_restore_snapshot` với `rpo_seconds`, `docs_lost`, `embed_model_version` | on-call |
| 4 | Scale pool warm→full | tự chạy bên trong runbook; chạy tay: ghi `full` vào `state/region-b/pool_state`, rồi `curl.exe -s localhost:8002/readyz` | `/readyz` của B trả HTTP 200 (`step:4_wait_ready` ok, vài giây warm-up) | on-call |
| 5 | DNS/LB cutover (CHỈ sau bước 4) | tự chạy bên trong runbook (bước `5_dns_cutover`) | `curl.exe -s localhost:8080/edge/state` cho `active_region=b` (chờ tối đa TTL 5s) | on-call |
| 6 | Verify golden signals | tự chạy: 10 request thật vào B; tay: `curl.exe -s "localhost:8080/v1/infer?q=test"` | `step:6` có `error_rate` = 0 và `p95_latency_ms` < 1000ms; `edge_region":"b"` | on-call |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` = `PASS` (hoặc `FAIL`), `valid:true`, `warnings` rỗng; mở `reports/postmortem.md` | on-call + chủ dịch vụ |

**Nếu bước 4 timeout (B không ready sau 60s):** script tự ABORT và KHÔNG cutover
(`active_region` giữ nguyên). Đừng đổi `edge/active_region` bằng tay — B chưa ready thì
user nhận 503 từ cả hai phía. Xem `reports/failover-events.jsonl` dòng `4_wait_ready` để biết lý do
(`pool_state`, `model_weights_missing`, `vector_db_empty`).

**Rollback (failover ngược về A):** chỉ khi đồng thời đủ cả ba điều kiện:
1. A trả `/readyz` 200 ổn định ít nhất 10 phút liên tục (health checker báo `HEALTHY` cho A);
2. dữ liệu ghi trên B trong lúc failover đã được đồng bộ ngược về A (không rollback nếu sẽ mất doc);
3. rollback thực hiện trong khung giờ ít tải, và có người trực xác nhận.

Người quyết định: **incident commander (chủ dịch vụ)**, không phải script. Không bật tự động
failover ngược (full-auto không có circuit breaker → hai region flap qua lại). Lệnh: restore
A (`python chaos/kill_region.py restore --region a --backend bare` trong lab; ở production là bật lại
instance), chạy lại runbook với `--primary b --target a`.
