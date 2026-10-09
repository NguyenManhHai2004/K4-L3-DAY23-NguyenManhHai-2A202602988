"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    ts = time.time()
    ev = {"ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    print(json.dumps(ev), flush=True)
    return ev


def state_of(region: str) -> dict:
    return httpx.get(f"{URL[region]}/v1/state", timeout=3).json()


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước đúng thứ tự; bước 4 timeout -> ABORT, không cutover."""
    source = "a" if target == "b" else "b"
    result = {"ok": False, "target": target, "steps": []}

    # 1. verify_target: region đích phải trả lời được (có thể còn rỗng/warm)
    try:
        st = state_of(target)
    except Exception as e:
        emit(step="1_verify_target", ok=False, target=target, error=type(e).__name__)
        result["error"] = f"target_unreachable: {type(e).__name__}"
        return result
    emit(step="1_verify_target", ok=True, target=target, state=st)
    result["steps"].append("1_verify_target")

    # 2. restore_snapshot + RPO + embed_model_version
    meta = snapshot.get(target, backend)
    r = snapshot.rpo(pathlib.Path(f"state/region-{source}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", ok=True, target=target, backend=backend,
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"))
    result.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"])
    result["steps"].append("2_restore_snapshot")

    # 3. scale_pool: warm -> full (kích hoạt warm-up)
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full", encoding="utf-8")
    emit(step="3_scale_pool", ok=True, target=target, pool_state="full")
    result["steps"].append("3_scale_pool")

    # 4. wait_ready: poll /readyz tới khi 200
    t0 = time.time()
    ready, last = False, None
    while time.time() - t0 < wait:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2)
            last = resp.status_code
            if resp.status_code == 200:
                ready = True
                break
        except Exception as e:
            last = type(e).__name__
        time.sleep(min(0.5, max(0.0, wait - (time.time() - t0))))
    waited = round(time.time() - t0, 2)
    if not ready:
        emit(step="4_wait_ready", ok=False, target=target, waited_s=waited, last=last,
             note="ABORT: target chua ready -> KHONG cutover")
        result["error"] = "target_not_ready"
        return result
    emit(step="4_wait_ready", ok=True, target=target, waited_s=waited)
    result["steps"].append("4_wait_ready")

    # 5. dns_cutover: chỉ sau khi target ready
    pathlib.Path("edge/active_region").write_text(target, encoding="utf-8")
    emit(step="5_dns_cutover", ok=True, target=target)
    result["steps"].append("5_dns_cutover")
    result["ok"] = True
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
