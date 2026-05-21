"""
SDO Dashboard API - Control Plane Backend tích hợp Process Control & Webhooks
"""

import asyncio
import datetime
import glob
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from orchestrator.config import DEFAULT_SERVICE, get_service_config, SERVICE_CONFIGS
from orchestrator.monitor import PrometheusMonitor

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

BASE_DIR = Path(os.getenv("SDO_WORKDIR", os.getcwd()))
LOGS_DIR = BASE_DIR / "logs"
TRAINING_DIR = BASE_DIR / "training_data"
WEBHOOKS_FILE = BASE_DIR / os.getenv("SDO_WEBHOOKS_FILE", "webhooks.json")
CONFIG_FILE = Path(os.getenv("SDO_CONFIG", "sdo.yml"))

# === SCHEMA ===
class WebhookStep(BaseModel):
    id: str = ""
    name: str = ""
    enabled: bool = True
    url: str = ""
    method: str = "POST"
    headers: str = "{}"
    body: str = "{}"
    body_mode: str = "json"
    extract: str = "{}"
    condition: str = ""
    timeout_seconds: int = 5
    continue_on_error: bool = False

class WebhookConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    events: List[str]
    variables: str = "{}"
    steps: List[WebhookStep]

class ServiceConfigUpdate(BaseModel):
    min_containers: Optional[int] = None
    max_containers: Optional[int] = None
    cpu_high: Optional[float] = None
    cpu_low: Optional[float] = None
    mem_danger: Optional[float] = None
    mem_warning: Optional[float] = None
    cooldown_seconds: Optional[int] = None
    rollback_window_seconds: Optional[int] = None
    rollback_error_threshold: Optional[float] = None
    rollback_latency_factor: Optional[float] = None
    rollback_action: Optional[str] = None
    tier: Optional[int] = None
    depends_on: Optional[List[str]] = None
    scalable: Optional[bool] = None

def _model_dump(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)

def _get_managed_services() -> List[str]:
    services = list(SERVICE_CONFIGS.keys())
    if not services:
        return [DEFAULT_SERVICE]
    return sorted(
        services,
        key=lambda name: (get_service_config(name).get("tier", 999), name),
    )

def _default_service() -> str:
    services = _get_managed_services()
    if DEFAULT_SERVICE in services:
        return DEFAULT_SERVICE
    return services[0] if services else DEFAULT_SERVICE

def _has_multi_service_topology() -> bool:
    return sum(1 for service in _get_managed_services() if get_service_config(service).get("tier") is not None) > 1

def _read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            return json.load(file_handle)
    except Exception:
        return fallback

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            for line in file_handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
    except Exception:
        return records
    return records

def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as file_handle:
        file_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

def _normalize_event_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload or {})
    normalized["event"] = str(normalized.get("event", "unknown") or "unknown").strip().lower()
    normalized["service"] = str(normalized.get("service", _default_service()) or _default_service())
    normalized["time"] = str(normalized.get("time", datetime.datetime.now().isoformat()) or datetime.datetime.now().isoformat())
    normalized.setdefault("action", "")
    normalized.setdefault("message", "")
    normalized.setdefault("reason", "")
    normalized.setdefault("state", {})
    normalized.setdefault("data", {})
    return normalized

def _incident_from_event(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    normalized = _normalize_event_payload(item)
    event = normalized.get("event")
    message = str(normalized.get("message") or normalized.get("reason") or "").strip()
    state = normalized.get("state") if isinstance(normalized.get("state"), dict) else {}
    data = normalized.get("data") if isinstance(normalized.get("data"), dict) else {}

    if event == "rollback_triggered":
        return {
            **normalized,
            "severity": "critical",
            "category": "rollback",
            "title": "Rollback Triggered",
            "message": message or f"Rollback action: {normalized.get('rollback_action', 'unknown')}",
            "source": "event",
            "state": state,
            "data": data,
        }

    if event == "agent_error":
        return {
            **normalized,
            "severity": "critical",
            "category": "agent_error",
            "title": "Agent Runtime Error",
            "message": message or "Unhandled exception occurred in agent loop",
            "source": "event",
            "state": state,
            "data": data,
        }

    if event == "service_degraded":
        return {
            **normalized,
            "severity": "warning",
            "category": "degradation",
            "title": "Service Degradation Detected",
            "message": message or "Service health or latency crossed warning thresholds",
            "source": "event",
            "state": state,
            "data": data,
        }

    return None

def _tail_log_lines(path: Path, limit: int = 200) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as file_handle:
            lines = file_handle.readlines()
    except Exception:
        return []
    return [line.rstrip("\r\n") for line in lines[-limit:]]

def _load_stored_incidents(limit: int = 0, service: Optional[str] = None) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for item in _read_jsonl(LOGS_DIR / "incidents.jsonl"):
        if service and service != "all" and item.get("service") != service:
            continue
        records.append(item)
    records.sort(key=lambda item: item.get("time", ""), reverse=True)
    if limit > 0:
        return records[:limit]
    return records

def _incident_from_action(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    state = item.get("state") or {}
    action = str(item.get("action", ""))
    source = str(item.get("decision_source", "")).lower()
    containers = int(state.get("containers", 0) or 0)
    healthy = int(state.get("healthy", containers) or containers)
    error_rate = float(state.get("error_rate", 0) or 0)
    delay_ms = float(state.get("delay_ms", 0) or 0)

    is_rollback = "rollback" in source
    is_disruptive = any(token in action for token in ("restart", "kill"))
    is_degraded = healthy < containers or error_rate > 0.1 or delay_ms > 1500
    if not (is_rollback or is_disruptive or is_degraded):
        return None

    reasons: List[str] = []
    if healthy < containers:
        reasons.append(f"Healthy replicas {healthy}/{containers}")
    if error_rate > 0.1:
        reasons.append(f"Error rate {(error_rate * 100):.1f}%")
    if delay_ms > 1500:
        reasons.append(f"Latency {delay_ms:.0f}ms")
    if is_disruptive:
        reasons.append(f"Disruptive action {action}")

    category = "rollback" if is_rollback else "degradation"
    if healthy < containers:
        category = "availability"
    elif error_rate > 0.1:
        category = "error_rate"
    elif delay_ms > 1500:
        category = "latency"

    return {
        **item,
        "severity": "critical" if is_rollback or error_rate > 0.3 else "warning",
        "title": "Rollback Triggered" if is_rollback else "Service Degradation Detected",
        "service": item.get("service", _default_service()),
        "message": "; ".join(reasons) or "Service state crossed an incident threshold",
        "category": category,
        "source": "action_log",
    }

def _load_action_history(limit: int = 0, service: Optional[str] = None) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not LOGS_DIR.exists():
        return records

    for pattern in ("agent_log_*.json", "multi_agent_log_*.json"):
        for file_path in sorted(LOGS_DIR.glob(pattern)):
            payload = _read_json(file_path, [])
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict):
                    continue
                normalized = dict(item)
                normalized.setdefault("service", _default_service())
                normalized.setdefault("mode", "unknown")
                normalized.setdefault("state", {})
                if service and service != "all" and normalized.get("service") != service:
                    continue
                records.append(normalized)

    records.sort(key=lambda item: item.get("time", ""))
    if limit > 0:
        return records[-limit:]
    return records

def _build_incidents(limit: int = 50, service: Optional[str] = None) -> List[Dict[str, Any]]:
    stored_incidents = _load_stored_incidents(limit=max(limit * 2, 100) if limit > 0 else 0, service=service)
    stored_fingerprints = {
        (
            str(item.get("service", "")),
            str(item.get("category", "")),
            str(item.get("time", ""))[:16],
        )
        for item in stored_incidents
    }

    incidents: List[Dict[str, Any]] = list(stored_incidents)
    for item in reversed(_load_action_history(limit=500, service=service)):
        incident = _incident_from_action(item)
        if not incident:
            continue
        fingerprint = (
            str(incident.get("service", "")),
            str(incident.get("category", "")),
            str(incident.get("time", ""))[:16],
        )
        if fingerprint in stored_fingerprints and incident.get("category") in {"rollback", "agent_error"}:
            continue
        incidents.append(incident)

    incidents.sort(key=lambda item: item.get("time", ""), reverse=True)
    if limit > 0:
        return incidents[:limit]
    return incidents

def _latest_training_file(prefix: str) -> Optional[str]:
    if not TRAINING_DIR.exists():
        return None
    matches = sorted(TRAINING_DIR.glob(f"{prefix}*.json"))
    if not matches:
        return None
    return str(matches[-1])

@app.get("/api/services")
def list_services():
    services = _get_managed_services()
    return {
        "default_service": _default_service(),
        "services": [
            {
                "name": service,
                "tier": get_service_config(service).get("tier"),
                "depends_on": get_service_config(service).get("depends_on", []),
                "scalable": get_service_config(service).get("scalable", True),
            }
            for service in services
        ],
    }

@app.get("/api/services/overview")
def get_services_overview(limit: int = Query(default=12, ge=1, le=50)):
    monitor = PrometheusMonitor()
    services = _get_managed_services()
    if _has_multi_service_topology():
        states = monitor.get_multi_state(services)
    else:
        states = {services[0]: monitor.get_state(services[0])}

    action_limit = max(limit * max(len(services), 1), 40)
    recent_actions = _load_action_history(limit=action_limit, service="all")
    recent_incidents = _build_incidents(limit=action_limit, service="all")

    actions_by_service: Dict[str, List[Dict[str, Any]]] = {}
    for item in reversed(recent_actions):
        actions_by_service.setdefault(item.get("service", _default_service()), []).append(item)

    incidents_by_service: Dict[str, List[Dict[str, Any]]] = {}
    for item in recent_incidents:
        incidents_by_service.setdefault(item.get("service", _default_service()), []).append(item)

    response_services: List[Dict[str, Any]] = []
    for service in services:
        state = states.get(service) or monitor.get_state(service)
        response_services.append({
            "service": service,
            "config": get_service_config(service),
            "state": state.to_dict(),
            "recent_actions": actions_by_service.get(service, [])[:limit],
            "recent_incidents": incidents_by_service.get(service, [])[: min(limit, 6)],
        })

    return {
        "agent": get_agent_status(),
        "services": response_services,
    }

def _get_agent_status():
    if agent_process and agent_process.poll() is None:
        mode = "live"
        try:
            cmd = agent_process.args
            if isinstance(cmd, list):
                if "--dry-run" in cmd:
                    mode = "dry-run"
                elif "--rules" in cmd:
                    mode = "rule-based"
        except Exception:
            pass
        return {"running": True, "mode": mode}
    return {"running": False, "mode": "stopped"}


@app.get("/api/agent/status")
def get_agent_status():
    return _get_agent_status()

@app.get("/api/agent/logs")
def get_agent_logs(source: str = Query(default="agent"), limit: int = Query(default=200, ge=20, le=2000)):
    sources = {
        "agent": LOGS_DIR / "agent_console.log",
        "events": LOGS_DIR / "agent_events.jsonl",
        "incidents": LOGS_DIR / "incidents.jsonl",
        "webhooks": LOGS_DIR / "webhook_runs.jsonl",
    }
    if source not in sources:
        raise HTTPException(400, "Unknown log source")
    path = sources[source]
    return {
        "source": source,
        "path": str(path),
        "exists": path.exists(),
        "updated_at": datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat() if path.exists() else None,
        "lines": _tail_log_lines(path, limit=limit),
    }

@app.get("/api/webhooks")
def list_webhooks():
    data = _read_json(WEBHOOKS_FILE, [])
    return data if isinstance(data, list) else []

@app.post("/api/webhooks")
def save_webhooks(webhooks: List[WebhookConfig]):
    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = [_model_dump(webhook) for webhook in webhooks]
    with open(WEBHOOKS_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, indent=2, ensure_ascii=False)
    return {"status": "success"}

@app.post("/api/agent/control")
def control_agent(action: str, mode: str = "live", service: Optional[str] = None):
    global agent_process, last_agent_start

    with agent_lock:
        if action == "stop":
            if agent_process and agent_process.poll() is None:
                agent_process.terminate()
                agent_process.wait()
            return {"status": "success", "message": "Agent stopped"}

        if action != "start":
            raise HTTPException(400, "Unknown agent action")

        if time.time() - last_agent_start < 5:
            return {"status": "cooldown", "message": "Agent is still cooling down"}

        if agent_process and agent_process.poll() is None:
            return {
                "status": "already_running",
                "message": "Agent already running"
            }

        if mode not in {"live", "dry-run", "rule-based"}:
            raise HTTPException(400, "Unknown agent mode")

        last_agent_start = time.time()
        cmd = [sys.executable, "-m", "orchestrator.main"]
        if _has_multi_service_topology():
            cmd.append("multi-agent")
        else:
            cmd.extend(["agent", "--service", service or _default_service()])

        if mode == "dry-run":
            cmd.append("--dry-run")
        elif mode == "rule-based":
            cmd.append("--rules")

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(LOGS_DIR / "agent_console.log", "a", encoding="utf-8", buffering=1)
        log_file.write("\n\n==================================\n")
        log_file.write(f"AGENT STARTED AT {datetime.datetime.now().isoformat()}\n")
        log_file.write("==================================\n")
        log_file.flush()

        agent_process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=(os.name != "nt"),
        )
        return {"status": "success", "command": cmd}

async def broadcast_training_log(text: str):
    stale_clients: List[WebSocket] = []
    for ws in list(training_clients):
        try:
            await ws.send_text(text)
        except Exception:
            stale_clients.append(ws)
    for ws in stale_clients:
        if ws in training_clients:
            training_clients.remove(ws)

@app.websocket("/api/training/stream")
async def training_stream(websocket: WebSocket):
    await websocket.accept()
    training_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in training_clients:
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

    cmd = [sys.executable, "-m", "orchestrator.main"]
    if action == "chaos":
        if _has_multi_service_topology():
            cmd.extend(["collect-multi", os.getenv("SDO_COLLECT_EPISODES", "20")])
        else:
            cmd.extend(["collect", os.getenv("SDO_COLLECT_EPISODES", "20"), "--service", _default_service()])
    elif action == "train":
        if _has_multi_service_topology():
            data_file = _latest_training_file("multi_chaos_")
            if not data_file:
                raise HTTPException(400, "Chưa có multi-service training data. Hãy chạy Chaos Collection trước.")
            cmd.extend(["train-offline-multi", data_file])
        else:
            data_file = _latest_training_file("chaos_data_")
            if not data_file:
                raise HTTPException(400, "Chưa có training data. Hãy chạy Chaos Collection trước.")
            cmd.extend(["train-offline", data_file])
    elif action == "export-onnx":
        cmd.extend(["export-onnx"])
    else:
        raise HTTPException(400, "Unknown action")

    training_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def log_reader(proc):
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                asyncio.run(broadcast_training_log(line))
        asyncio.run(broadcast_training_log(f"\n[SYSTEM] Tiến trình kết thúc với code {proc.poll()}\n"))

    threading.Thread(target=log_reader, args=(training_process,), daemon=True).start()
    return {"status": "success", "command": cmd}

@app.post("/api/agent-events")
def receive_agent_event(payload: Dict[str, Any]):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    normalized_payload = _normalize_event_payload(payload)
    _append_jsonl(LOGS_DIR / "agent_events.jsonl", normalized_payload)
    incident = _incident_from_event(normalized_payload)
    if incident:
        _append_jsonl(LOGS_DIR / "incidents.jsonl", incident)
    return {"status": "accepted", "event": normalized_payload.get("event")}

@app.get("/api/system/summary")
def get_system_summary():
    monitor = PrometheusMonitor()
    services = _get_managed_services()
    if _has_multi_service_topology():
        states = monitor.get_multi_state(services)
    else:
        states = {services[0]: monitor.get_state(services[0])}

    host_state = next(iter(states.values())) if states else monitor.get_state(_default_service())
    rollbacks = sum(1 for item in _load_action_history(limit=200) if "rollback" in str(item.get("decision_source", "")).lower())
    active_alerts = sum(
        1 for state in states.values()
        if state.error_rate > 0.1 or state.healthy_count < state.container_count or state.response_time_ms > 1500
    )

    return {
        "host_cpu_available": host_state.host_cpu_available,
        "host_mem_available": host_state.host_mem_available,
        "total_services_managed": len(services),
        "agent_mode": get_agent_status()["mode"],
        "recent_rollbacks": rollbacks,
        "active_alerts": active_alerts,
        "services": [
            {
                "service": service,
                "tier": get_service_config(service).get("tier"),
                "cpu_percent": state.cpu_percent,
                "memory_percent": state.memory_percent,
                "error_rate": state.error_rate,
                "healthy_count": state.healthy_count,
                "container_count": state.container_count,
            }
            for service, state in states.items()
        ],
    }

@app.get("/api/service/state")
def get_serv_state(service: str = Query(default=None)):
    service = service or _default_service()
    return {"service": service, "state": PrometheusMonitor().get_state(service).to_dict()}

@app.get("/api/service/actions")
def get_acts(limit: int = 50, service: str = Query(default="all")):
    return _load_action_history(limit=limit, service=service)

@app.get("/api/incidents")
def get_incidents(limit: int = 50, service: str = Query(default="all")):
    return _build_incidents(limit=limit, service=service)

@app.get("/api/service")
def get_cfg(service: str = Query(default=None)):
    service = service or _default_service()
    return {"service": service, "config": get_service_config(service)}

@app.post("/api/service/config")
def upd_cfg(p: ServiceConfigUpdate, service: str = Query(default=None)):
    service = service or _default_service()
    patch = _model_dump(p)
    if not patch:
        raise HTTPException(400, "Không có thay đổi cấu hình để lưu")

    data = {"services": {service: patch}}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as file_handle:
            data = yaml.safe_load(file_handle) or {}
        if "services" not in data:
            data["services"] = {}
        if service not in data["services"]:
            data["services"][service] = {}
        data["services"][service].update(patch)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as file_handle:
        yaml.safe_dump(data, file_handle, sort_keys=False)
    SERVICE_CONFIGS[service] = {**SERVICE_CONFIGS.get(service, {}), **patch}
    return {"status": "ok", "service": service, "config": get_service_config(service)}
