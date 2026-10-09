# Postmortem — DR Drill Lab 23

Blameless: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".
Thời gian là UTC (giờ local = +7h). Đây là drill có kiểm soát: Region A bị chặn mạng (`netblock`) có chủ đích.

## 1. Timeline (mọi dòng có evidence path:line)

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 04:32:01.6 | outage bắt đầu (kill Region A, netblock) | `chaos/chaos-events.jsonl:6` |
| 04:32:01.6 | user đầu tiên bị ảnh hưởng (ReadTimeout, +0.1s) | `reports/drill-2-withdr.jsonl:25` |
| 04:32:16.5 | health check alert: Region A UNHEALTHY (+14.9s) | `reports/health-events.jsonl:2` |
| 04:32:24.3 | operator confirm / bắt đầu failover (+22.7s) | `reports/runbook-run.jsonl:2` |
| 04:32:31.5 | DNS cutover sang B sau khi B ready (+29.9s) | `reports/failover-events.jsonl:5` |
| 04:32:33.1 | resolved: request đầu tiên OK từ Region B (+31.6s) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `31.6s` · gap: `268.4s` (còn dư — đạt mục tiêu). Nguồn: `reports/measure-drill-2.json:20`
- RPO mục tiêu: 300s · đo được: `16.01s` (`8` doc bị mất) · gap: `283.99s` (đạt mục tiêu). Nguồn: `reports/failover-events.jsonl:2`
- Baseline không DR: `NO_RECOVERY`, 14 request fail và không bao giờ hồi phục (`reports/measure-drill-1.json:25`).
- **Bước tốn nhiều giây nhất:** phát hiện outage — `14.9s` (47% RTO), vì `interval × threshold = 5s × 3 = 15s` là
  sàn cứng của health checker. Ngay sau đó là `8.1s` giữa lúc phát hiện và lúc failover thật sự bắt đầu
  (runbook re-probe 3 lần tuần tự, mỗi lần timeout 2s; xem `reports/runbook-run.jsonl:1`), rồi GPU warm-up `6.85s`
  (`reports/failover-events.jsonl:4`).

## 3. Root cause (5 whys)

Câu hỏi: nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?

1. **Vì sao user thấy lỗi ~30s?** Vì Region A ngừng trả lời và traffic vẫn đi vào A cho tới khi cutover.
2. **Vì sao traffic vẫn vào A?** Vì chuyển traffic phụ thuộc health checker phát hiện (sàn 15s) rồi failover; edge chỉ đổi region khi `active_region` đổi.
3. **Vì sao Region B không nhận traffic ngay?** Vì B là *warm standby* rỗng (`count 0`, không weights, pool `warm`) — nó chưa thể serve; process sống ≠ có thể serve (`/readyz` 503).
4. **Vì sao B rỗng?** Vì dữ liệu chỉ được đồng bộ bằng snapshot định kỳ (30s) lên object store và chỉ restore khi failover — chọn warm-standby để tiết kiệm chi phí GPU/storage.
5. **Vì sao vậy?** Vì thiết kế ưu tiên chi phí hơn RTO/RPO bằng 0; RPO = chu kỳ replicate (8 doc mất trong lab) và RTO = detect + restore + warm-up, chưa có replication liên tục hay pool nóng sẵn.

**Bước trong runbook sẽ thất bại nếu outage thật** (các rủi ro tôi tìm thấy khi làm lab):
- Snapshot chưa từng được tạo → `failover` chết ở `2_restore_snapshot`. Bài học: giám sát tuổi snapshot (RPO), không chỉ giả định replicate đang chạy. (Lần drill đầu RPO 1713s vì snapshot đầu tiên chạy trước khi có doc mới.)
- Cutover sớm hơn phát hiện làm số đo vô nghĩa (cảnh báo `t_cutover < t_detect`) và nếu B chưa ready thì user nhận 503 từ cả hai phía. Đã xử lý: failover chỉ cutover sau `/readyz` 200; runbook chờ alert.
- Tooling chỉ chạy trên Linux (PID, `SIGSTOP`) làm drill thất bại im lặng trên Windows — runbook phải được thử trên đúng môi trường vận hành.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Runbook bỏ re-probe tuần tự khi đã có alert từ health checker (hoặc probe song song) | Nguyen Manh Hai | 2026-10-16 | RTO −~8s |
| 2 | Hạ interval xuống 2s, giữ threshold 3 + thêm hysteresis chống flapping | on-call SRE | 2026-10-23 | RTO −9s (detect 15s → 6s) |
| 3 | Giảm chu kỳ replicate 30s → 10s và cảnh báo khi tuổi snapshot > 60s | Data/Platform owner | 2026-10-23 | RPO −~20s |
| 4 | Pre-warm GPU pool của B (giữ pool ở mức warm cao) | Platform owner | 2026-11-06 | RTO −~5s (warm-up 6.85s) |
| 5 | Giảm `EDGE_TTL_SECONDS` 5s → 2s | Edge owner | 2026-10-16 | RTO −~1s |
| 6 | Viết CI test chạy drill tự động trên đúng nền tảng vận hành (gồm Windows) | Nguyen Manh Hai | 2026-10-30 | Phát hiện sớm lỗi runbook |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của tôi là **15s** (5s × 3). Nó chiếm **47% RTO** (14.9s / 31.6s). Nếu tính cả nó là sàn thấp nhất cho bất kỳ phương án nào dùng health check này.
2. Hạ interval xuống 1s (threshold 3) thì detect floor còn 3s, RTO giảm khoảng **12s** (15s → 3s). Đánh đổi: nhiều probe hơn, một lỗi mạng ngắn (vài giây) có thể kích hoạt failover giả và dẫn tới flapping giữa hai region (§4 Anti-Patterns), nên cần hysteresis, ngưỡng recover và bước confirm của con người.
3. Nếu outage kéo dài 6 giờ và Region A mất dữ liệu vĩnh viễn, `docs_lost = 8` nghĩa là 8 tài liệu được ingest sau snapshot cuối **không còn ở đâu cả** — với khách hàng đó là dữ liệu hoá đơn/ticket đã xác nhận nhưng biến mất. Trong lab số này nhỏ (16s dữ liệu) vì ingest chỉ 0.5 doc/s; ở tải thật nó tăng tuyến tính theo chu kỳ replicate × tốc độ ingest, nên cần nguồn ghi lại (replay log/queue) để bù, hoặc replication đồng bộ cho dữ liệu quan trọng.
