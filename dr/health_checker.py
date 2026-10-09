"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """(ready, reason). Dung /readyz; timeout bat buoc vi netblock lam request treo."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ok"
    try:
        reasons = r.json().get("reasons")
    except Exception:
        reasons = None
    return False, f"http_{r.status_code}" + (f": {','.join(reasons)}" if reasons else "")


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll ca 2 region; chi doi trang thai sau `threshold` lan fail LIEN TIEP."""
    out.parent.mkdir(parents=True, exist_ok=True)
    state = {r: "HEALTHY" for r in URL}   # gia dinh healthy luc bat dau
    fails = {r: 0 for r in URL}
    passes = {r: 0 for r in URL}
    end = time.time() + duration
    with out.open("a", encoding="utf-8") as f:
        while time.time() < end:
            t0 = time.time()
            for region in URL:
                ok, reason = probe(region, timeout)
                if ok:
                    fails[region] = 0
                    passes[region] += 1
                else:
                    passes[region] = 0
                    fails[region] += 1
                new = None
                if not ok and state[region] == "HEALTHY" and fails[region] >= threshold:
                    new = "UNHEALTHY"
                elif ok and state[region] == "UNHEALTHY" and passes[region] >= threshold:
                    new = "HEALTHY"
                if new:
                    state[region] = new
                    ev = {"ts": time.time(), "event": "state_change", "region": region,
                          "to": new, "reason": reason, "interval_s": interval,
                          "threshold": threshold, "consecutive_fails": fails[region]}
                    f.write(json.dumps(ev) + "\n")
                    f.flush()
                    print(json.dumps(ev), flush=True)
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
