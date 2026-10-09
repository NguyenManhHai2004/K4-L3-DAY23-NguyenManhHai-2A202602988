"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    ts = time.time()
    ev = {"ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
          "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    print(json.dumps(ev), flush=True)
    return ev


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _ready(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=2).status_code == 200
    except Exception:
        return False


HEALTH_WAIT_S = 45
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")


def _wait_alert(region: str, t_outage, wait: float):
    """Chờ dòng state_change UNHEALTHY của region (ts >= t_outage) trong health log."""
    if t_outage is None:
        return None
    end = time.time() + wait
    while True:
        if HEALTH_LOG.exists():
            for line in HEALTH_LOG.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                        and e.get("region") == region and e["ts"] >= t_outage):
                    return e["ts"]
        if time.time() >= end:
            return None
        time.sleep(1)


def _t_outage(region: str):
    """ts của lần kill gần nhất cho `region` trong chaos-events (None nếu không có)."""
    f = pathlib.Path("chaos/chaos-events.jsonl")
    if not f.exists():
        return None
    ts = None
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("action") == "kill" and e.get("region") == region:
            ts = e["ts"]
    return ts


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook; mỗi bước 1 dòng log."""
    # 1. xác nhận outage: nhiều lần probe, không tin 1 lần fail
    # Bán tự động: đợi health checker (nếu đang chạy) báo UNHEALTHY cho primary SAU t_outage,
    # để cutover không đi trước detect. Không có alert trong HEALTH_WAIT_S -> chỉ dựa vào probe.
    t_out = _t_outage(primary)
    alert = _wait_alert(primary, t_out, HEALTH_WAIT_S)
    probes = [_ready(primary) for _ in range(3)]
    outage = not any(probes)
    step(1, "xac_nhan_outage", primary=primary, probes=probes, outage_confirmed=outage,
         health_alert_ts=alert, target_ready=_ready(target))
    if not outage:
        return {"ok": False, "aborted": "primary_van_ready", "primary": primary}

    # 2. thông báo incident: mốc operator biết tin (sau t_outage)
    t_known = time.time()
    t_outage = _t_outage(primary)
    step(2, "thong_bao_incident", t_outage=t_outage, t_known=t_known,
         notify_delay_s=None if t_outage is None else round(t_known - t_outage, 2))
    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(7, "post_incident", ok=False, note="operator tu choi failover")
        return {"ok": False, "aborted": "operator_declined"}

    # 3. scale_gpu_pool: failover() đúng MỘT lần
    res = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=res.get("ok"), steps_done=res.get("steps"),
         error=res.get("error"))

    # 4. verify_state_replica: chỉ đọc kết quả bước 3 (+ trạng thái region phụ)
    try:
        st = fo.state_of(target)
    except Exception as e:
        st = {"error": type(e).__name__}
    step(4, "verify_state_replica", rpo_seconds=res.get("rpo_seconds"),
         docs_lost=res.get("docs_lost"), count=st.get("count"), weights=st.get("weights"),
         pool_state=st.get("pool_state"))

    # 5. dns_cutover: chỉ đọc lại kết quả
    cut = "5_dns_cutover" in (res.get("steps") or [])
    step(5, "dns_cutover", ok=cut and bool(res.get("ok")), target=target)

    # 6. golden signals: 10 request thật vào region phụ
    lat, errs = [], 0
    for _ in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", params={"q": "hoa don thang 7"}, timeout=5)
            if r.status_code != 200:
                errs += 1
        except Exception:
            errs += 1
        lat.append((time.time() - t0) * 1000)
    lat.sort()
    p95 = round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1)
    step(6, "verify_golden_signals", requests=10, errors=errs, error_rate=errs / 10,
         p95_latency_ms=p95)

    # 7. post_incident
    elapsed = round(time.time() - t_known, 2)
    ok = bool(res.get("ok")) and errs == 0
    step(7, "post_incident", ok=ok, elapsed_s=elapsed,
         measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return {"ok": ok, "elapsed_s": elapsed, "failover": res, "p95_latency_ms": p95,
            "error_rate": errs / 10}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
