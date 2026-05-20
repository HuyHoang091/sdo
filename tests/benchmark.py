import os
import sys
import time
import json
import glob
from datetime import datetime
import pandas as pd
import matplotlib.pyplot as plt

# Add orchestrator to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.docker_ctl import DockerController
from orchestrator.chaos import ChaosInjector

# ============
# DATA & SCENARIOS
# ============
# Chứa tổng quan 20 kịch bản theo yêu cầu
# FP: 2, 3, 11, 13, 16, 17
# CASCADE: 9, 10, 15, 18, 20
# BN (Bottleneck): 8, 12

EPISODES_DEF = [
    {"ep": 1, "fault_type": "cpu_stress", "target": "api", "flags": []},
    {"ep": 2, "fault_type": "fp", "target": "web-app", "flags": ["FP"]},
    {"ep": 3, "fault_type": "fp", "target": "api", "flags": ["FP"]},
    {"ep": 4, "fault_type": "make_unhealthy", "target": "web-app", "flags": []},
    {"ep": 5, "fault_type": "make_unhealthy", "target": "api", "flags": []},
    {"ep": 6, "fault_type": "network_chaos", "target": "web-app", "flags": []},
    {"ep": 7, "fault_type": "network_chaos", "target": "db", "flags": []}, # Episode 7: DB network chaos
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
    "fp": 1 # Ko có yêu cầu delay
}

EXPECTED_ACTIONS = {
    "cpu_stress": ["scale_up", "increase_cpu_limit"],
    "memory_leak": ["kill_and_replace", "increase_mem_limit"],
    "network_chaos": ["kill_and_replace", "scale_up", "no_op"],
    "make_unhealthy": ["restart_unhealthy"],
    "crash_container": ["restart_unhealthy", "kill_and_replace"],
}

WRONG_ACTIONS = {
    "cpu_stress": ["scale_down", "restart_unhealthy", "kill_and_replace"],
    "network_chaos": ["scale_down"],
    "make_unhealthy": ["scale_down", "scale_up"],
    "crash_container": ["scale_down"],
}

# ============
# HELPER FUNCTIONS
# ============

import requests

def get_agent_metrics():
    try:
        r = requests.get("http://127.0.0.1:9200/metrics", timeout=2)
        return r.text
    except Exception:
        return ""

def parse_actions_from_metrics(metrics_text: str):
    """Parse Prometheus exposition to dict of actions"""
    actions = {}
    for line in metrics_text.splitlines():
        if line.startswith("sdo_agent_actions_total{"):
            try:
                part1, value = line.split("} ")
                service = part1.split('service="')[1].split('"')[0]
                action = part1.split('action="')[1].split('"')[0]
                actions[f"{service}:{action}"] = int(float(value))
            except:
                pass
    return actions

# ============
# EVALUATION LOGIC
# ============

def evaluate_episode(ep_def: dict, actions_log: list):
    """Chấm điểm tập dữ liệu agent sequence dựa trên P1, P2, P3 định nghĩa."""
    
    # Pre-extract data
    fault_type = ep_def["fault_type"]
    target_svc = ep_def["target"]
    is_fp = "FP" in ep_def["flags"]
    is_cascade = "CASCADE" in ep_def["flags"]
    is_bn = "BN" in ep_def["flags"]
    
    # Metrics
    p1 = 0.0 # Perception
    p2 = 0.0 # Action Quality
    p3 = 0.0 # Timing
    info = {"note": "", "correct_action_step": -1, "fp_penalty": False}
    
    # Filter valid actions (loại bỏ no_op thông thường của RL)
    actual_actions = [x for x in actions_log if not x["action"].startswith("no_op")]
    
    # ==========================
    # 1. P1: PERCEPTION SCORE
    # ==========================
    if is_fp:
        # Nếu là False Positive
        if len(actual_actions) == 0 or all(x["action"].startswith("scale_down") for x in actual_actions):
            p1 = 1.0 # Chuẩn (im lặng hoặc scale down)
        else:
            p1 = 0.0
            info["fp_penalty"] = True # Phạt tổng nếu tác động làm sai
    else:
        # Nếu có lỗi
        if len(actual_actions) == 0:
            p1 = 0.0 # Không nhận ra fault
        else:
            first_act = actual_actions[0]
            # Nhận đúng/sai service
            if first_act["service"] == target_svc:
                # Nhận đúng service. Nhưng có đúng fault type ko?
                # Kiểm tra fault type bằng cách xem xét expected_action (nếu làm đúng semantic action thì suy là nhận đúng fault)
                if first_act["action"] in EXPECTED_ACTIONS.get(fault_type, []):
                    p1 = 1.0
                else:
                    p1 = 0.8 # Đúng service, hơi sai kiểu
            else:
                p1 = 0.5 # Sai service affected (hoặc CASCADE affected sớm)
                if is_cascade and first_act["service"] != target_svc:
                    # Cascade: target là DB nhưng phạt api
                    p1 = 0.5
                # Nếu hành động lên service KHÔNG bị ảnh hưởng (khỏe) -> -0.3
                # Vì kịch bản chúng ta tạo là 1 svc lỗi, các svc khác có thể ảnh. Nếu nó act vào svc hoàn toàn unrelated thì -0.3.
                if not is_cascade and first_act["service"] != target_svc:
                    p1 = -0.3
                    
    # ==========================
    # 2. P2: ACTION QUALITY
    # ==========================
    if is_fp:
        p2 = 1.0 if not info["fp_penalty"] else 0.0
    else:
        penalty = 0.0
        corrects = 0
        total_acts = max(len(actual_actions), 1) if actual_actions else 1
        
        for act in actual_actions:
            name = act["action"]
            if name in EXPECTED_ACTIONS.get(fault_type, []):
                corrects += 1
                if info["correct_action_step"] == -1:
                    info["correct_action_step"] = act["step"]
            elif name in WRONG_ACTIONS.get(fault_type, []):
                penalty += 0.1
                if name == "scale_up" and ep_def["ep"] == 7: # Hard code check episode 7 wrong attribution
                    info["note"] = "Wrong attribution (Ep 7)"
        
        if len(actual_actions) == 0:
            p2 = 0.0
        else:
            p2 = (corrects / len(actual_actions)) - penalty
            p2 = max(0.0, min(1.0, p2)) # Clip 0-1
        
    # ==========================
    # 3. P3: TIMING SCORE
    # ==========================
    if is_fp:
        p3 = 1.0
    else:
        if info["correct_action_step"] != -1:
            # Ngưỡng fault clear theo yêu cầu user
            if fault_type == "cpu_stress":
                start_fault = 6 # Rõ ràng từ step 6-7
            elif fault_type == "memory_leak":
                start_fault = 4 
            else:
                start_fault = 0 # Network/Crash/Unhealthy là rõ ngay

            reaction_delay = info["correct_action_step"] - start_fault
            
            # Tốc độ phản ứng
            max_tol = MAX_TOLERABLE_DELAY.get(fault_type, 1)
            ts = 1.0 - (max(0, reaction_delay) / max_tol)
            p3 = max(0.0, ts)
        else:
            p3 = 0.0 # Không sửa lỗi
        
    return p1, p2, p3, info

# ============
# RUNNER
# ============

def run_live_benchmark():
    docker_ctl = DockerController(compose_file=os.path.join("..", "docker-compose.yml"))
    chaos = ChaosInjector(docker_ctl)

    print("=== SDO RL Agent: KÍCH HOẠT BENCHMARK TOÀN DIỆN (20 EPISODES) ===")
    print("Mỗi kịch bản sẽ bơm lỗi và thu thập Metric theo cơ chế tính điểm P1-P2-P3.")
    
    print(f"Bắt đầu Benchmark Metrics Live HTTP Polling...")
    
    results_data = []
    
    for ep in EPISODES_DEF:
        print(f"\n[Episode {ep['ep']}/20] Injecting {ep['fault_type']} on {ep['target']} (Flags: {ep['flags']})")
        
        chaos.cleanup(ep["target"])
        time.sleep(2) # Chờ xíu
        
        # Bơm lỗi (Nếu không phải FP)
        fault = ep["fault_type"]
        if fault == "cpu_stress":
            chaos.cpu_stress(ep["target"], percent=90, duration=30)
        elif fault == "memory_leak":
            chaos.memory_leak(ep["target"], mb_per_step=15, duration=30)
        elif fault == "network_chaos":
            chaos.network_chaos(ep["target"], profile="random", duration=30)
        elif fault == "make_unhealthy":
            chaos.make_unhealthy(ep["target"])
        elif fault == "crash_container":
            chaos.crash_container(ep["target"])
        elif fault == "fp":
            pass # Không làm gì, để hệ thống yên
            
        print("  -> Chờ 100s để quan sát Agent xử lý...")
        
        actions_log = []
        baseline_actions = parse_actions_from_metrics(get_agent_metrics())
        start_time = time.time()
        
        # Poll 100s
        for _ in range(50):
            time.sleep(2)
            current_actions = parse_actions_from_metrics(get_agent_metrics())
            for key, val in current_actions.items():
                if val > baseline_actions.get(key, 0):
                    diff = val - baseline_actions.get(key, 0)
                    svc, act = key.split(":")
                    if not act.startswith("no_op"):
                        # Record!
                        step_passed = int((time.time() - start_time) / 5.0)
                        for __ in range(diff):
                            actions_log.append({
                                "step": step_passed,
                                "service": svc,
                                "action": act
                            })
                    baseline_actions[key] = val
                    
        chaos.cleanup(ep["target"]) # Xong là gỡ lỗi, chuẩn bị sang tập tiếp
        
        # CHẤM ĐIỂM
        p1, p2, p3, info = evaluate_episode(ep, actions_log)
        
        info_step = info["correct_action_step"]
        print(f"  => Score: P1={p1:.2f} | P2={p2:.2f} | P3={p3:.2f}. Actions logs count: {len(actions_log)} (React at step: {info_step})")
        if info["note"]: print(f"  !!! Note: {info['note']}")
        
        results_data.append({
            "Ep": ep["ep"],
            "Type": ep["fault_type"],
            "Target": ep["target"],
            "Flags": ", ".join(ep["flags"]),
            "P1": p1,
            "P2": p2,
            "P3": p3,
            "FP_Penalty": info["fp_penalty"]
        })
        
        time.sleep(30) # Chờ 30s để hệ thống dọn dẹp sạch sẽ Chaos và ổn định lại trước khi sang Episode tiếp theo
        
    # ============
    # COMPOSITE SCORE
    # ============
    print("\n==== TỔNG KẾT & TÍNH FINAL SCORE ====")
    df = pd.DataFrame(results_data)
    
    # Averages
    avg_p1 = df["P1"].mean()
    avg_p2 = df["P2"].mean()
    avg_p3 = df["P3"].mean()
    
    base_score = (avg_p1 * 0.35 + avg_p2 * 0.35 + avg_p3 * 0.20) * 100
    
    # Bonuses
    # Cascade bonus (+5 cho mỗi tập cascade làm tốt P1 > 0)
    cascade_bonus = 0
    cascade_eps = df[df["Flags"].str.contains("CASCADE")]
    for _, row in cascade_eps.iterrows():
        if row["P1"] >= 1.0:
            cascade_bonus += 5
            
    # BN bonus (+3 cho mỗi BN làm tốt)
    bn_bonus = 0
    bn_eps = df[df["Flags"].str.contains("BN")]
    for _, row in bn_eps.iterrows():
        if row["P1"] >= 1.0 or row["P2"] > 0:
            bn_bonus += 3
            
    # FP Penalty
    fp_eps = df[df["Type"] == "fp"]
    fp_fail_count = fp_eps["FP_Penalty"].sum()
    fp_rate = fp_fail_count / max(len(fp_eps), 1)
    fp_penalty_val = (fp_fail_count / max(len(fp_eps), 1)) * 10.0 # 10% penalty
    
    final_score = base_score + cascade_bonus + bn_bonus - fp_penalty_val
    final_score = max(0, min(100, final_score)) # clamp 0-100
    
    # Xếp loại
    lbl = ""
    if final_score >= 85: lbl = "Production-ready"
    elif final_score >= 70: lbl = "Acceptable, cần fine-tune FP threshold"
    elif final_score >= 55: lbl = "Cần retrain thêm cascade/network episodes"
    else: lbl = "Insufficient — review reward function"
    
    print(f"Base Score Component: P1={avg_p1:.2f}, P2={avg_p2:.2f}, P3={avg_p3:.2f}")
    print(f"Base Formual Unscaled: {(avg_p1 * 0.35 + avg_p2 * 0.35 + avg_p3 * 0.20):.2f}")
    print(f"Bonuses: Cascade= +{cascade_bonus}, BN= +{bn_bonus}")
    print(f"Penalty: FP Rate= {fp_rate*100:.0f}% -> -{fp_penalty_val}")
    print(f"FINAL COMPOSITE SCORE: {final_score:.2f} / 100")
    print(f"Phân loại: {lbl}")
    
    # Export reports
    with open("evaluation_report.md", "w", encoding="utf-8") as f:
        f.write("# Báo Cáo Đánh Giá RL Agent\n\n")
        f.write("Dựa trên hệ thống chấm thầu 3 trục: Perception (P1), Action Quality (P2), Timing (P3).\n\n")
        f.write(f"### Điểm tổng hợp (Composite Score): **{final_score:.2f} / 100**\n")
        f.write(f"**Đánh giá:** {lbl}\n\n")
        
        f.write("### Chi tiết từng Episode\n\n")
        f.write(df[["Ep", "Type", "Target", "Flags", "P1", "P2", "P3"]].to_markdown(index=False, floatfmt=".2f"))
        f.write("\n\n### Chỉ số Bonus/Penalty\n")
        f.write(f"- Cascade Root Cause Bonus: +{cascade_bonus}\n")
        f.write(f"- Bottleneck Detection Bonus: +{bn_bonus}\n")
        f.write(f"- False Positive Penalty: -{fp_penalty_val} (Rate: {fp_rate*100:.0f}%)\n")
        
    print("\n[+] ĐÃ LƯU BÁO CÁO VÀO: tests/evaluation_report.md")

if __name__ == "__main__":
    run_live_benchmark()
