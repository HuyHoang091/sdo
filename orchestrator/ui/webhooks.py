"""
SDO Webhooks - Gửi sự kiện action/rollback ra hệ thống bên ngoài.

Cấu hình qua env:
  SDO_WEBHOOK_URLS="https://example.com/hook1,https://example.com/hook2"

Payload mẫu:
{
  "event": "action_performed" | "rollback_triggered",
  "service": "my-app",
  "time": "2026-03-03T10:00:00",
  "data": { ... tuỳ sự kiện ... }
}
"""

import json
import os
import requests
import queue
import threading
from typing import Any, Dict
from orchestrator.config import logger as root_logger

log = root_logger.getChild("webhooks")
WEBHOOKS_FILE = "webhooks.json"

# ==========================================
# Giao việc gửi Webhook cho Background Worker Queue
# ==========================================
webhook_queue = queue.Queue()

def _webhook_worker():
    """Worker thread chạy ngầm, liên tục lấy job từ queue để gửi đi"""
    while True:
        try:
            job = webhook_queue.get()
            if job is None:
                continue
                
            hook_config, payload_str, service, current_time = job
            for step in hook_config.get("steps", []):
                try:
                    headers = json.loads(step.get("headers") or "{}")
                    body_str = step.get("body", "{}")
                    
                    # Placeholder replacements
                    body_str = body_str.replace("{{action}}", payload_str)
                    body_str = body_str.replace("{{time}}", current_time)
                    body_str = body_str.replace("{{service}}", service)
                    
                    req_body = json.loads(body_str) if body_str else None
                    
                    resp = requests.request(
                        method=step.get("method", "POST"),
                        url=step["url"],
                        headers=headers,
                        json=req_body,
                        timeout=5
                    )
                    
                    if resp.status_code >= 400:
                        log.warning("Webhook '%s' step failed HTTP %s", hook_config.get("name"), resp.status_code)
                except Exception as err:
                    log.warning("Webhook '%s' failed: %s", hook_config.get("name"), err)
        except Exception as e:
            log.error("Webhook worker error: %s", e)
        finally:
            webhook_queue.task_done()

# Khởi tạo worker ngay khi module load
worker_thread = threading.Thread(target=_webhook_worker, daemon=True)
worker_thread.start()

def has_webhooks() -> bool:
    if not os.path.exists(WEBHOOKS_FILE):
        return False
    try:
        with open(WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            hooks = json.load(f)
            return len(hooks) > 0
    except:
        return False

def send_event(event: str, payload: Dict[str, Any]) -> None:
    """
    Called by LiveAgent to broadcast system events or direct actions to user-defined webhooks.
    """
    if not os.path.exists(WEBHOOKS_FILE):
        return
        
    try:
        with open(WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            webhooks = json.load(f)
    except Exception as e:
        log.warning("Failed to parse %s: %s", WEBHOOKS_FILE, e)
        return

    action_str = str(payload.get("action", "")).lower()
    service_name = payload.get("service", "unknown")
    event_time = payload.get("time", "")

    for hk in webhooks:
        hk_events = [e.strip().lower() for e in hk.get("events", [])]
        
        is_triggered = False
        # Condition 1: Match generic event like 'action_performed' or 'rollback_triggered'
        if event in hk_events:
            is_triggered = True
        else:
            # Condition 2: Match specific action names (e.g. 'scale_up', 'no_op')
            for e in hk_events:
                if e == action_str or (e in action_str and e != ""):
                    is_triggered = True
                    break
                    
        if is_triggered:
            # Chỉ đẩy việc vào Queue, không tạo Thread hay block Agent loop
            webhook_queue.put((hk, action_str, service_name, event_time))

