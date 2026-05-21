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
import queue
import threading
from pathlib import Path
from typing import Any, Dict

import requests
from orchestrator.config import logger as root_logger

log = root_logger.getChild("webhooks")
WEBHOOKS_FILE = Path(os.getenv("SDO_WEBHOOKS_FILE", "webhooks.json"))
WEBHOOK_LOG_FILE = Path(os.getenv("SDO_WEBHOOK_LOG_FILE", "logs/webhook_runs.jsonl"))

# ==========================================
# Giao việc gửi Webhook cho Background Worker Queue
# ==========================================
webhook_queue = queue.Queue()

def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as file_handle:
        file_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

def _load_file_webhooks() -> list[dict]:
    if not WEBHOOKS_FILE.exists():
        return []
    try:
        with open(WEBHOOKS_FILE, "r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
        return payload if isinstance(payload, list) else []
    except Exception as err:
        log.warning("Failed to parse %s: %s", WEBHOOKS_FILE, err)
        return []

def _env_webhook_urls() -> list[str]:
    raw_value = os.getenv("SDO_WEBHOOK_URLS", "")
    return [url.strip() for url in raw_value.split(",") if url.strip()]

def _deep_get(value: Any, path: str, default: Any = "") -> Any:
    current = value
    for token in [part for part in str(path or "").split(".") if part]:
        if isinstance(current, dict):
            current = current.get(token, default)
            continue
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return default
            continue
        return default
    return current

def _flatten_context(prefix: str, value: Any, output: Dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            _flatten_context(next_prefix, item, output)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            next_prefix = f"{prefix}.{index}" if prefix else str(index)
            _flatten_context(next_prefix, item, output)
        return
    output[prefix] = "" if value is None else str(value)

def _render_template(template: str, context: Dict[str, Any]) -> str:
    output = template or ""
    flat_context: Dict[str, str] = {}
    _flatten_context("", context, flat_context)
    for key in sorted(flat_context.keys(), key=len, reverse=True):
        if not key:
            continue
        output = output.replace(f"{{{{{key}}}}}", flat_context[key])
    return output

def _parse_json_string(raw_value: Any, fallback: Any) -> Any:
    if isinstance(raw_value, (dict, list)):
        return raw_value
    if not isinstance(raw_value, str) or not raw_value.strip():
        return fallback
    try:
        return json.loads(raw_value)
    except Exception:
        return fallback

def _build_context(event_name: str, payload: Dict[str, Any], hook_config: Dict[str, Any], workflow_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event": event_name,
        "action": str(payload.get("action", "")),
        "service": str(payload.get("service", "unknown")),
        "time": str(payload.get("time", "")),
        "payload": payload,
        "data": payload.get("data", {}),
        "workflow": workflow_state,
        "hook": {
            "id": hook_config.get("id", ""),
            "name": hook_config.get("name", ""),
        },
        "steps": workflow_state.get("steps", {}),
    }

def _evaluate_condition(condition: str, context: Dict[str, Any]) -> bool:
    rendered = _render_template(condition or "", context).strip()
    if not rendered:
        return True
    return rendered.lower() not in {"0", "false", "no", "skip", "null", "none"}

def _extract_values(step_extract: Any, response: requests.Response) -> Dict[str, Any]:
    extract_map = _parse_json_string(step_extract, {})
    if not isinstance(extract_map, dict):
        return {}

    try:
        response_json = response.json()
    except ValueError:
        response_json = {}

    response_context = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "text": response.text,
        "json": response_json,
    }
    extracted: Dict[str, Any] = {}
    for key, path in extract_map.items():
        extracted[str(key)] = _deep_get(response_context, str(path), "")
    return extracted

def _log_webhook_run(payload: Dict[str, Any]) -> None:
    try:
        _append_jsonl(WEBHOOK_LOG_FILE, payload)
    except Exception as err:
        log.warning("Failed to write webhook run log: %s", err)

def _webhook_worker():
    """Worker thread chạy ngầm, liên tục lấy job từ queue để gửi đi"""
    while True:
        job = webhook_queue.get()
        try:
            if job is None:
                continue

            job_type, target, event_name, payload = job
            workflow_state: Dict[str, Any] = {
                "variables": _parse_json_string(target.get("variables", "{}") if isinstance(target, dict) else "{}", {}),
                "steps": {},
                "last": {},
            }

            if job_type == "direct_url":
                response = requests.post(target, json=payload, timeout=5)
                if response.status_code >= 400:
                    log.warning("Webhook '%s' failed HTTP %s", target, response.status_code)
                _log_webhook_run({
                    "type": "direct_url",
                    "target": target,
                    "event": event_name,
                    "service": payload.get("service", "unknown"),
                    "time": payload.get("time", ""),
                    "status_code": response.status_code,
                })
                continue

            hook_config = target
            for step in hook_config.get("steps", []):
                try:
                    if not step.get("enabled", True):
                        continue

                    step_id = str(step.get("id") or step.get("name") or f"step_{len(workflow_state['steps']) + 1}")
                    context = _build_context(event_name, payload, hook_config, workflow_state)
                    if not _evaluate_condition(step.get("condition", ""), context):
                        workflow_state["steps"][step_id] = {"skipped": True, "reason": "condition=false"}
                        continue

                    headers_str = _render_template(step.get("headers") or "{}", context)
                    body_str = _render_template(step.get("body") or "", context)
                    url = _render_template(step.get("url") or "", context)
                    headers = json.loads(headers_str) if headers_str else {}

                    request_kwargs: Dict[str, Any] = {
                        "method": step.get("method", "POST"),
                        "url": url,
                        "headers": headers,
                        "timeout": int(step.get("timeout_seconds", 5) or 5),
                    }

                    if body_str:
                        if str(step.get("body_mode", "json")).lower() == "text":
                            request_kwargs["data"] = body_str.encode("utf-8")
                        else:
                            try:
                                request_kwargs["json"] = json.loads(body_str)
                            except json.JSONDecodeError:
                                request_kwargs["data"] = body_str.encode("utf-8")
                    elif step.get("method", "POST").upper() != "GET":
                        request_kwargs["json"] = payload

                    resp = requests.request(**request_kwargs)
                    extracted = _extract_values(step.get("extract", "{}"), resp)
                    workflow_state["variables"].update(extracted)
                    workflow_state["last"] = {
                        "status_code": resp.status_code,
                        "text": resp.text,
                        "headers": dict(resp.headers),
                        "json": _parse_json_string(resp.text, {}),
                    }
                    workflow_state["steps"][step_id] = {
                        "status_code": resp.status_code,
                        "headers": dict(resp.headers),
                        "text": resp.text,
                        "json": _parse_json_string(resp.text, {}),
                        "extracted": extracted,
                    }
                    _log_webhook_run({
                        "type": "workflow",
                        "hook": hook_config.get("name", ""),
                        "step_id": step_id,
                        "event": event_name,
                        "service": payload.get("service", "unknown"),
                        "time": payload.get("time", ""),
                        "status_code": resp.status_code,
                        "url": url,
                        "extracted": extracted,
                    })
                    if resp.status_code >= 400:
                        log.warning("Webhook '%s' step failed HTTP %s", hook_config.get("name"), resp.status_code)
                        if not step.get("continue_on_error", False):
                            break
                except Exception as err:
                    log.warning("Webhook '%s' failed: %s", hook_config.get("name"), err)
                    _log_webhook_run({
                        "type": "workflow",
                        "hook": hook_config.get("name", ""),
                        "step_id": step.get("id") or step.get("name") or "unknown",
                        "event": event_name,
                        "service": payload.get("service", "unknown"),
                        "time": payload.get("time", ""),
                        "error": str(err),
                    })
                    if not step.get("continue_on_error", False):
                        break
        except Exception as err:
            log.error("Webhook worker error: %s", err)
        finally:
            webhook_queue.task_done()

# Khởi tạo worker ngay khi module load
worker_thread = threading.Thread(target=_webhook_worker, daemon=True)
worker_thread.start()

def has_webhooks() -> bool:
    return bool(_env_webhook_urls() or _load_file_webhooks())

def send_event(event: str, payload: Dict[str, Any]) -> None:
    """
    Called by LiveAgent to broadcast system events or direct actions to user-defined webhooks.
    """
    if not has_webhooks():
        return

    normalized_event = str(event or "").strip().lower()
    normalized_payload = dict(payload or {})
    normalized_payload["event"] = normalized_event
    normalized_payload.setdefault("action", "")
    normalized_payload.setdefault("service", "unknown")
    normalized_payload.setdefault("time", "")
    normalized_payload.setdefault("data", {})

    action_str = str(normalized_payload.get("action", "")).strip().lower()

    for url in _env_webhook_urls():
        webhook_queue.put(("direct_url", url, normalized_event, normalized_payload))

    for hook_config in _load_file_webhooks():
        hook_events = [str(item).strip().lower() for item in hook_config.get("events", []) if str(item).strip()]

        is_triggered = normalized_event in hook_events
        if not is_triggered and action_str:
            for hook_event in hook_events:
                if hook_event == action_str or hook_event in action_str:
                    is_triggered = True
                    break

        if is_triggered:
            webhook_queue.put(("workflow", hook_config, normalized_event, normalized_payload))
