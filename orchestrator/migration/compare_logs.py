import json
import os

def load_actions(logfile_path):
    actions = []
    if not os.path.exists(logfile_path):
        return actions
    with open(logfile_path, "r") as f:
        for line in f:
            if "ACTION" in line:
                try:
                    parts = line.split("ACTION: ")[1].strip()
                    # Parse assuming format: "Service: web-app | Action: scale_up | Context: ..."
                    service = parts.split("|")[0].split(":")[1].strip()
                    action = parts.split("|")[1].split(":")[1].strip()
                    actions.append((service, action))
                except:
                    pass
    return actions

def compare(v1_log, v2_log):
    v1_actions = load_actions(v1_log)
    v2_actions = load_actions(v2_log)
    
    print(f"Total V1 Actions: {len(v1_actions)}")
    print(f"Total V2 Actions: {len(v2_actions)}")
    
    match_count = 0
    for a1, a2 in zip(v1_actions, v2_actions):
        if a1 == a2:
            match_count += 1
            
    min_len = min(len(v1_actions), len(v2_actions))
    if min_len > 0:
        match_rate = (match_count / min_len) * 100
        print(f"Action Matching Rate (V1 vs V2): {match_rate:.2f}%")
        if match_rate < 80.0:
            print("WARNING: Significant deviation detected between MultiServiceAgent and CoreAgent decisions!")
            
if __name__ == "__main__":
    # Thay đổi path tuỳ theo cấu hình hệ thống hiện tại
    compare("logs/v1_agent.log", "logs/sdo_core.log")
