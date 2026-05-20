"""
K8s Benchmark Runner — Chạy cùng 20 episodes trên K8s Kind cluster.
Đo hành vi phản ứng của K8s native (liveness probe restart + HPA scale)
và chấm điểm P1/P2/P3 theo cùng tiêu chuẩn với SDO RL Agent.
"""

import os
import sys
import time
import json
import subprocess
import logging
from datetime import datetime

# Đảm bảo kubectl nằm trong PATH (cài ở ~/.local/bin)
_bin_dir = os.path.join(os.path.expanduser("~"), ".local", "bin")
if _bin_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _bin_dir + os.pathsep + os.environ.get("PATH", "")

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import k8s_chaos as chaos

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("k8s.bench")

NAMESPACE = "sdo-benchmark"

# ============================================================
# Cùng 20 kịch bản với benchmark.py gốc
# ============================================================
EPISODES_DEF = [
    {"ep": 1, "fault_type": "cpu_stress", "target": "api", "flags": []},
    {"ep": 2, "fault_type": "fp", "target": "web-app", "flags": ["FP"]},
    {"ep": 3, "fault_type": "fp", "target": "api", "flags": ["FP"]},
    {"ep": 4, "fault_type": "make_unhealthy", "target": "web-app", "flags": []},
    {"ep": 5, "fault_type": "make_unhealthy", "target": "api", "flags": []},
    {"ep": 6, "fault_type": "network_chaos", "target": "web-app", "flags": []},
    {"ep": 7, "fault_type": "network_chaos", "target": "db", "flags": []},
    {"ep": 8, "fault_type": "cpu_stress", "target": "api", "flags": ["BN"]},
    {"ep": 9, "fault_type": "cpu_stress", "target": "db", "flags": ["CASCADE"]},
    {"ep": 10, "fault_type": "make_unhealthy", "target": "db", "flags": ["CASCADE"]},
    {"ep": 11, "fault_type": "fp", "target": "db", "flags": ["FP"]},
    {"ep": 12, "fault_type": "memory_leak", "target": "api", "flags": ["BN"]},
    {"ep": 13, "fault_type": "fp", "target": "api", "flags": ["FP"]},
    {"ep": 14, "fault_type": "crash_container", "target": "api", "flags": []},
    {"ep": 15, "fault_type": "crash_container", "target": "db", "flags": ["CASCADE"]},
    {"ep": 16, "fault_type": "fp", "target": "web-app", "flags": ["FP"]},
    {"ep": 17, "fault_type": "fp", "target": "db", "flags": ["FP"]},
    {"ep": 18, "fault_type": "network_chaos", "target": "db", "flags": ["CASCADE"]},
    {"ep": 19, "fault_type": "memory_leak", "target": "web-app", "flags": []},
    {"ep": 20, "fault_type": "cpu_stress", "target": "db", "flags": ["CASCADE"]},
]

MAX_TOLERABLE_DELAY = {
    "cpu_stress": 3,
    "memory_leak": 3,
    "network_chaos": 1,
    "make_unhealthy": 1,
    "crash_container": 1,
    "fp": 1,
}

# K8s native hành vi ánh xạ
# K8s chỉ có 2 loại phản ứng: restart (qua liveness fail) và scale (qua HPA)
K8S_EXPECTED_REACTIONS = {
    "cpu_stress":      "hpa_scale",       # HPA scale up khi CPU > 80%
    "memory_leak":     "restart",          # OOMKill hoặc liveness fail -> restart
    "network_chaos":   None,               # K8s KHÔNG CÓ reaction native
    "make_unhealthy":  "restart",          # Liveness probe fail -> restart
    "crash_container": "restart",          # Pod crash -> restartPolicy restart
}

# ============================================================
# HELPERS
# ============================================================

def kubectl_json(*args):
    cmd = ["kubectl", "-n", NAMESPACE] + list(args) + ["-o", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except:
        pass
    return None

def get_pod_restart_counts(app_label: str) -> dict:
    """Trả về {pod_name: restart_count}."""
    data = kubectl_json("get", "pods", "-l", f"app={app_label}")
    if not data:
        return {}
    result = {}
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        statuses = item.get("status", {}).get("containerStatuses", [])
        rc = statuses[0]["restartCount"] if statuses else 0
        result[name] = rc
    return result

def get_hpa_replicas(hpa_name: str) -> int:
    """Lấy số replicas hiện tại từ HPA."""
    data = kubectl_json("get", "hpa", hpa_name)
    if data:
        return data.get("status", {}).get("currentReplicas", 1)
    return 1

def get_pod_ready_count(app_label: str) -> int:
    """Đếm số pods Ready."""
    data = kubectl_json("get", "pods", "-l", f"app={app_label}")
    if not data:
        return 0
    count = 0
    for item in data.get("items", []):
        conditions = item.get("status", {}).get("conditions", [])
        for c in conditions:
            if c["type"] == "Ready" and c["status"] == "True":
                count += 1
    return count

def check_any_pod_not_ready(app_label: str) -> bool:
    """Kiểm tra có pod nào NotReady không."""
    data = kubectl_json("get", "pods", "-l", f"app={app_label}")
    if not data:
        return False
    for item in data.get("items", []):
        conditions = item.get("status", {}).get("conditions", [])
        for c in conditions:
            if c["type"] == "Ready" and c["status"] != "True":
                return True
        # Pod không có condition Ready
        phase = item.get("status", {}).get("phase", "")
        if phase not in ("Running",):
            return True
    return False

# ============================================================
# SCORING (ánh xạ K8s behavior sang P1/P2/P3)
# ============================================================

def evaluate_k8s_episode(ep_def: dict, events: dict) -> tuple:
    """
    events = {
        "restart_detected_step": int or -1,
        "hpa_scaled_step": int or -1,
        "not_ready_detected_step": int or -1,
        "any_action": bool,
        "fp_acted": bool,
    }
    """
    fault_type = ep_def["fault_type"]
    is_fp = "FP" in ep_def["flags"]
    is_cascade = "CASCADE" in ep_def["flags"]
    
    p1, p2, p3 = 0.0, 0.0, 0.0
    info = {"note": "", "correct_action_step": -1, "fp_penalty": False}
    
    expected = K8S_EXPECTED_REACTIONS.get(fault_type)
    
    # ================= P1: PERCEPTION =================
    if is_fp:
        if not events["fp_acted"]:
            p1 = 1.0  # Đúng: không hành động khi không có lỗi
        else:
            p1 = 0.0
            info["fp_penalty"] = True
    else:
        if events["not_ready_detected_step"] >= 0 or events["restart_detected_step"] >= 0:
            p1 = 1.0  # K8s phát hiện được (qua probe)
            # Nhưng K8s không biết phân biệt loại lỗi
            if expected is None:  # network_chaos
                p1 = 0.5  # K8s detect nhưng không hiểu đúng loại lỗi
        else:
            if expected is not None:
                p1 = 0.0  # Không phát hiện
            else:
                p1 = 0.5  # Network chaos -> K8s không detect
    
    # ================= P2: ACTION QUALITY =================
    if is_fp:
        p2 = 1.0 if not info["fp_penalty"] else 0.0
    else:
        if expected == "restart":
            if events["restart_detected_step"] >= 0:
                p2 = 1.0
                info["correct_action_step"] = events["restart_detected_step"]
            elif events["hpa_scaled_step"] >= 0:
                p2 = 0.4
                info["correct_action_step"] = events["hpa_scaled_step"]
                info["note"] = "K8s HPA scaled thay vì Restart (Do CPU side-effect)"
            else:
                p2 = 0.0
        elif expected == "hpa_scale":
            if events["hpa_scaled_step"] >= 0:
                p2 = 1.0
                info["correct_action_step"] = events["hpa_scaled_step"]
            elif events["restart_detected_step"] >= 0:
                # K8s restart thay vì scale -> nửa đúng
                p2 = 0.4
                info["correct_action_step"] = events["restart_detected_step"]
                info["note"] = "K8s restart thay vì scale (thiếu hành vi)"
            else:
                p2 = 0.0
        elif expected is None:
            # Network chaos: K8s không có hành động native
            p2 = 0.0
            info["note"] = "K8s không hỗ trợ xử lý network chaos"
    
    # ================= P3: TIMING =================
    if is_fp:
        p3 = 1.0
    else:
        step = info["correct_action_step"]
        if step >= 0:
            if fault_type == "cpu_stress":
                start_fault = 6
            elif fault_type == "memory_leak":
                start_fault = 4
            else:
                start_fault = 0
            
            reaction_delay = step - start_fault
            max_tol = MAX_TOLERABLE_DELAY.get(fault_type, 1)
            ts = 1.0 - (max(0, reaction_delay) / max_tol)
            p3 = max(0.0, ts)
        else:
            p3 = 0.0
    
    return p1, p2, p3, info

# ============================================================
# MAIN RUNNER
# ============================================================

def run_k8s_benchmark():
    print("=" * 60)
    print(" K8S BENCHMARK: Đánh giá Kubernetes Native Healing")
    print(" Cluster: kind (sdo-bench)")
    print("=" * 60)
    
    results = []
    
    for ep in EPISODES_DEF:
        fault = ep["fault_type"]
        target = ep["target"]
        is_fp = "FP" in ep["flags"]
        
        print(f"\n[K8s Episode {ep['ep']}/20] Injecting {fault} on {target} (Flags: {ep['flags']})")
        
        # Cleanup trước
        chaos.cleanup(target)
        time.sleep(5)
        
        # Snapshot trạng thái trước injection
        baseline_restarts = get_pod_restart_counts(target)
        hpa_name = f"{target}-hpa" if target != "db" else None
        baseline_replicas = get_hpa_replicas(hpa_name) if hpa_name else 1
        
        # --- INJECT ---
        if fault == "cpu_stress":
            chaos.cpu_stress(target, percent=90)
        elif fault == "memory_leak":
            chaos.memory_leak(target, mb_per_step=15)
        elif fault == "network_chaos":
            chaos.network_chaos(target, profile="random")
        elif fault == "make_unhealthy":
            chaos.make_unhealthy(target)
        elif fault == "crash_container":
            chaos.crash_container(target)
        elif fault == "fp":
            pass  # Không làm gì
        
        print(f"  -> Chờ 100s để quan sát K8s phản ứng...")
        
        # --- OBSERVATION (100s) ---
        events = {
            "restart_detected_step": -1,
            "hpa_scaled_step": -1,
            "not_ready_detected_step": -1,
            "any_action": False,
            "fp_acted": False,
        }
        
        start_time = time.time()
        
        for poll in range(50):
            time.sleep(2)
            step = int((time.time() - start_time) / 5.0)
            
            # Check restarts
            current_restarts = get_pod_restart_counts(target)
            for pod, rc in current_restarts.items():
                old_rc = baseline_restarts.get(pod, 0)
                if rc > old_rc and events["restart_detected_step"] < 0:
                    events["restart_detected_step"] = step
                    events["any_action"] = True
                    log.info("  K8s RESTART detected for %s at step %d (count: %d->%d)", pod, step, old_rc, rc)
            
            # Check HPA scale
            if hpa_name:
                current_replicas = get_hpa_replicas(hpa_name)
                if current_replicas > baseline_replicas and events["hpa_scaled_step"] < 0:
                    events["hpa_scaled_step"] = step
                    events["any_action"] = True
                    log.info("  K8s HPA SCALE detected for %s at step %d (replicas: %d->%d)", target, step, baseline_replicas, current_replicas)
            
            # Check not ready
            if check_any_pod_not_ready(target) and events["not_ready_detected_step"] < 0:
                events["not_ready_detected_step"] = step
        
        # FP check
        if is_fp and events["any_action"]:
            events["fp_acted"] = True
        
        # Cleanup
        chaos.cleanup(target)
        
        # Score
        p1, p2, p3, info = evaluate_k8s_episode(ep, events)
        
        print(f"  => K8s Score: P1={p1:.2f} | P2={p2:.2f} | P3={p3:.2f}")
        if info["note"]:
            print(f"  !!! {info['note']}")
        
        results.append({
            "Ep": ep["ep"],
            "Type": fault,
            "Target": target,
            "Flags": ", ".join(ep["flags"]),
            "P1": p1,
            "P2": p2,
            "P3": p3,
            "FP_Penalty": info["fp_penalty"],
        })
        
        time.sleep(30)  # Ổn định
    
    # ============================================================
    # COMPOSITE SCORE
    # ============================================================
    print("\n" + "=" * 60)
    print(" TỔNG KẾT K8S BENCHMARK")
    print("=" * 60)
    
    df = pd.DataFrame(results)
    
    avg_p1 = df["P1"].mean()
    avg_p2 = df["P2"].mean()
    avg_p3 = df["P3"].mean()
    
    base_score = (avg_p1 * 0.35 + avg_p2 * 0.35 + avg_p3 * 0.20) * 100
    
    # Bonuses
    cascade_bonus = 0
    cascade_eps = df[df["Flags"].str.contains("CASCADE")]
    for _, row in cascade_eps.iterrows():
        if row["P1"] >= 1.0:
            cascade_bonus += 5
    
    bn_bonus = 0
    bn_eps = df[df["Flags"].str.contains("BN")]
    for _, row in bn_eps.iterrows():
        if row["P1"] >= 1.0 or row["P2"] > 0:
            bn_bonus += 3
    
    fp_eps = df[df["Type"] == "fp"]
    fp_fail_count = fp_eps["FP_Penalty"].sum()
    fp_rate = fp_fail_count / max(len(fp_eps), 1)
    fp_penalty_val = fp_rate * 10.0
    
    final_score = base_score + cascade_bonus + bn_bonus - fp_penalty_val
    final_score = max(0, min(100, final_score))
    
    lbl = ""
    if final_score >= 85: lbl = "Production-ready"
    elif final_score >= 70: lbl = "Acceptable"
    elif final_score >= 55: lbl = "Basic coverage"
    else: lbl = "Insufficient"
    
    print(f"Base Score: P1={avg_p1:.2f}, P2={avg_p2:.2f}, P3={avg_p3:.2f}")
    print(f"Bonuses: Cascade=+{cascade_bonus}, BN=+{bn_bonus}")
    print(f"Penalty: FP Rate={fp_rate*100:.0f}% -> -{fp_penalty_val:.1f}")
    print(f"FINAL K8S SCORE: {final_score:.2f} / 100")
    print(f"Phân loại: {lbl}")
    
    # Export
    with open("k8s_evaluation_report.md", "w", encoding="utf-8") as f:
        f.write("# Báo Cáo Đánh Giá Kubernetes Native Healing\n\n")
        f.write("Cluster: kind (sdo-bench) — K8s native liveness/readiness probes + HPA\n\n")
        f.write(f"### Điểm tổng hợp (Composite Score): **{final_score:.2f} / 100**\n")
        f.write(f"**Đánh giá:** {lbl}\n\n")
        f.write("### Chi tiết từng Episode\n\n")
        f.write(df[["Ep", "Type", "Target", "Flags", "P1", "P2", "P3"]].to_markdown(index=False, floatfmt=".2f"))
        f.write(f"\n\n### Chỉ số Bonus/Penalty\n")
        f.write(f"- Cascade Root Cause Bonus: +{cascade_bonus}\n")
        f.write(f"- Bottleneck Detection Bonus: +{bn_bonus}\n")
        f.write(f"- False Positive Penalty: -{fp_penalty_val:.1f} (Rate: {fp_rate*100:.0f}%)\n")
    
    # Save raw data for comparison
    df.to_json("k8s_benchmark_results.json", orient="records", indent=2)
    
    print("\n[+] Đã lưu: k8s_evaluation_report.md + k8s_benchmark_results.json")

if __name__ == "__main__":
    run_k8s_benchmark()
