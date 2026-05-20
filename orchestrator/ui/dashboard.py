"""
SDO Dashboard API - Control Plane Backend tích hợp Process Control & Webhooks
"""

import os, sys
import json
import glob
import time
import yaml
import subprocess
import asyncio
import threading
import requests
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from orchestrator.config import DEFAULT_SERVICE, get_service_config, SERVICE_CONFIGS
from orchestrator.monitor import PrometheusMonitor

import threading

agent_lock = threading.Lock()
last_agent_start = 0

app = FastAPI(title="SDO Control Plane API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === TRẠNG THÁI TIẾN TRÌNH ===
agent_process: Optional[subprocess.Popen] = None
training_process: Optional[subprocess.Popen] = None
training_clients: List[WebSocket] = []

WEBHOOKS_FILE = "webhooks.json"

# === SCHEMA ===
class WebhookStep(BaseModel):
    url: str
    method: str = "POST"
    headers: str = "{}"
    body: str = "{}"

class WebhookConfig(BaseModel):
    id: str
    name: str
    events: List[str]
    steps: List[WebhookStep]

class ServiceConfigUpdate(BaseModel):
    min_containers: int
    max_containers: int
    cpu_high: float
    cpu_low: float
    cooldown_seconds: int
    rollback_window_seconds: int

# # ================================
# # WEBHOOK POLLER BACKGROUND TASK
# # ================================
# def poll_and_fire_webhooks():
#     """Chạy ngầm liên tục, đọc log mới nhất và bắn webhook tương ứng"""
#     last_action_time = None
#     while True:
#         try:
#             time.sleep(5)
#             if not os.path.exists(WEBHOOKS_FILE): continue
#             with open(WEBHOOKS_FILE, "r", encoding="utf-8") as f:
#                 webhooks = json.load(f)
            
#             logs_dir = "logs"
#             if not os.path.isdir(logs_dir): continue
            
#             files = sorted(glob.glob(os.path.join(logs_dir, "agent_log_*.json")), reverse=True)
#             if not files: continue
            
#             # Chỉ check file ghi gần nhất
#             try:
#                 with open(files[0], "r", encoding="utf-8") as f:
#                     actions = json.load(f)
#             except: continue
            
#             new_actions = [a for a in actions if a.get("time") and (not last_action_time or a["time"] > last_action_time)]
#             if new_actions:
#                 new_actions.sort(key=lambda x: x.get("time", ""))
#                 last_action_time = new_actions[-1]["time"]
                
#                 for act in new_actions:
#                     event_type = "action_performed"
#                     if "rollback" in str(act.get("decision_source", "")).lower():
#                         event_type = "rollback_triggered"
                    
#                     for hk in webhooks:
#                         if event_type in hk.get("events", []):
#                             # Thực thi chuỗi "Multi-step" webhooks
#                             for step in hk.get("steps", []):
#                                 try:
#                                     headers = json.loads(step.get("headers") or "{}")
#                                     body_str = step.get("body", "{}")
#                                     # Inject placeholder
#                                     body_str = body_str.replace("{{action}}", act.get("action", ""))
#                                     body_str = body_str.replace("{{time}}", act.get("time", ""))
#                                     body_str = body_str.replace("{{service}}", DEFAULT_SERVICE)
#                                     req_body = json.loads(body_str) if body_str else None
                                    
#                                     requests.request(
#                                         method=step.get("method", "POST"),
#                                         url=step["url"], headers=headers, json=req_body, timeout=5
#                                     )
#                                 except Exception as err:
#                                     print(f"Webhook {hk['name']} failed at step: {err}")
#         except Exception:
#             pass

# @app.on_event("startup")
# def startup_event():
#     # Khởi động thread lắng nghe webhook
#     import time
#     threading.Thread(target=poll_and_fire_webhooks, daemon=True).start()

# ================================
# AGENT & TRAINING CONTROL
# ================================
@app.get("/api/agent/status")
def get_agent_status():
    global agent_process
    if agent_process and agent_process.poll() is None:
        args = agent_process.args
        mode = "live"
        if "--dry-run" in args: mode = "dry-run"
        if "--rules" in args: mode = "rule-based"
        return {"running": True, "mode": mode}
    return {"running": False, "mode": "none"}

@app.post("/api/agent/control")
def control_agent(action: str, mode: str = "live"):
    global agent_process, last_agent_start

    with agent_lock:
        if action == "stop":
            if agent_process and agent_process.poll() is None:
                agent_process.terminate()
                agent_process.wait()
            return {"status": "success", "message": "Agent stopped"}
        
        if action == "start":
            if time.time() - last_agent_start < 5:
                return {"status": "cooldown", "message": "Agent is still cooling down"}

            if agent_process and agent_process.poll() is None:
                # agent_process.terminate()
                # agent_process.wait()
                return {
                    "status": "already_running",
                    "message": "Agent already running"
                }

            last_agent_start = time.time()
                
            cmd = [sys.executable, "-m", "orchestrator.main", "agent"]
            if mode == "dry-run": cmd.append("--dry-run")
            if mode == "rule-based": cmd.append("--rules")
            
            # Mở process ngầm
            # agent_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Mở process ngầm, ghi log console ra file để dễ theo dõi
            os.makedirs("logs", exist_ok=True)
            log_file = open("logs/agent_console.log", "a", encoding="utf-8", buffering=1)
            import datetime
            log_file.write(f"\n\n==================================\n")
            log_file.write(f"🚀 AGENT STARTED AT {datetime.datetime.now().isoformat()}\n")
            log_file.write(f"==================================\n")
            log_file.flush()
            
            agent_process = subprocess.Popen(
                cmd, 
                stdout=log_file, 
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=(os.name != "nt")
            )
            return {"status": "success"}

async def broadcast_training_log(text: str):
    for ws in training_clients:
        try: await ws.send_text(text)
        except: pass

@app.websocket("/api/training/stream")
async def training_stream(websocket: WebSocket):
    await websocket.accept()
    training_clients.append(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        training_clients.remove(websocket)

@app.post("/api/training/control")
async def control_training(action: str):
    global training_process
    if action == "stop":
        if training_process and training_process.poll() is None:
            training_process.terminate()
            await broadcast_training_log("\n[SYSTEM] Tiến trình đã bị hủy (Signal: SIGTERM / Ctrl+C)\n")
        return {"status": "success"}

    if training_process and training_process.poll() is None:
        raise HTTPException(400, "Một tiến trình khác đang chạy.")

    cmd = ["python", "-m", "orchestrator.main"]
    if action == "chaos": cmd.extend(["collect", "50"])
    elif action == "train": cmd.extend(["train-live"])
    elif action == "export-onnx": cmd.extend(["export-onnx"])
    else: raise HTTPException(400, "Unknown action")

    # Mở terminal stream
    training_process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    
    def log_reader(proc):
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None: break
            if line: asyncio.run(broadcast_training_log(line))
        asyncio.run(broadcast_training_log(f"\n[SYSTEM] Tiến trình kết thúc với code {proc.poll()}\n"))

    threading.Thread(target=log_reader, args=(training_process,), daemon=True).start()
    return {"status": "success"}

# ================================
# WEBHOOK CRUD
# ================================
@app.get("/api/webhooks")
def list_webhooks():
    if not os.path.exists(WEBHOOKS_FILE): return []
    with open(WEBHOOKS_FILE, "r") as f: return json.load(f)

@app.post("/api/webhooks")
def save_webhooks(webhooks: List[WebhookConfig]):
    with open(WEBHOOKS_FILE, "w") as f:
        json.dump([w.dict() for w in webhooks], f, indent=2)
    return {"status": "success"}

# (Giữ nguyên các API cũ về trạng thái và metrics)
@app.get("/api/system/summary")
def get_system_summary():
    """Lấy host summary metrics và alerts chung."""
    monitor = PrometheusMonitor()
    state = monitor.get_state(DEFAULT_SERVICE) # Lấy node stats
    logs_dir = "logs"
    errors = 0
    rollbacks = 0
    # Đếm qua history log nhanh...
    files = glob.glob(os.path.join(logs_dir, "agent_log_*.json"))
    for f in files[-1:]:  # Đọc file mới nhất cho lẹ
        try:
            with open(f, "r") as log_f:
                data = json.load(log_f)
                rollbacks += sum(1 for a in data if "rollback" in str(a.get("decision_source", "")))
        except: pass

    return {
        "host_cpu_available": state.host_cpu_available,
        "host_mem_available": state.host_mem_available,
        "total_services_managed": len(SERVICE_CONFIGS),
        "agent_mode": get_agent_status()["mode"],
        "recent_rollbacks": rollbacks,
        "active_alerts": 2 if state.error_rate > 0.1 else 0
    }

@app.get("/api/service/state")
def get_serv_state(service: str=DEFAULT_SERVICE):
    return {"service": service, "state": PrometheusMonitor().get_state(service).to_dict()}

@app.get("/api/service/actions")
def get_acts(limit: int = 50):
    try:
        files = glob.glob(os.path.join("logs", "agent_log_*.json"))
        if not files: return []
        with open(sorted(files)[-1], "r") as f: 
            data = json.load(f)
            return data[-limit:] if limit > 0 else data
    except: return []

@app.get("/api/service")
def get_cfg(service: str=DEFAULT_SERVICE):
    return {"service": service, "config": get_service_config(service)}

@app.post("/api/service/config")
def upd_cfg(p: ServiceConfigUpdate, service: str=DEFAULT_SERVICE):
    path = os.getenv("SDO_CONFIG", "sdo.yml")
    data = {"services": {service: p.dict()}}
    if os.path.exists(path):
        with open(path, "r") as f: data = yaml.safe_load(f) or {}
        if "services" not in data: data["services"] = {}
        if service not in data["services"]: data["services"][service] = {}
        data["services"][service].update(p.dict())
    with open(path, "w") as f: yaml.dump(data, f)
    return {"status": "ok"}
