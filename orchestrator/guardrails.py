"""
SDO Guardrails - An toàn & bảo vệ cho agent.

3 cơ chế:
1. Cooldown: Sau khi thực hiện action, chờ X giây trước khi cho phép action tiếp
2. Dry-run / Shadow mode: Chỉ log đề xuất, không chạm Docker
3. Rollback: Tự disable RL nếu tình trạng xấu đi

Rollback metrics hiện dựa trên: CPU, Memory, Health ratio.
TODO: Khi app có custom metrics (http_requests_total, http_request_duration_seconds),
      bổ sung PromQL queries để lấy error rate & p95 latency trực tiếp:
      - Error rate: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])
      - p95 latency: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))
      Hiện tại dùng CPU/MEM/health làm proxy vì chưa có app metrics exporter.
"""

import time
import logging
from typing import Optional, Tuple

from orchestrator.config import get_service_config

log = logging.getLogger("sdo.guardrails")


class CooldownManager:
    """
    Quản lý cooldown giữa các action.
    Cooldown duration đọc từ get_service_config(service)["cooldown_seconds"],
    cho phép mỗi service có cooldown khác nhau.
    """

    def __init__(self):
        # {service_name: timestamp_of_last_action}
        self._last_action_time: dict[str, float] = {}
        # {service_name: action_name_of_last_action}
        self._last_action_name: dict[str, str] = {}

    def can_act(self, service: str, state=None) -> Tuple[bool, float]:
        """
        Kiểm tra có được phép thực hiện action không.
        Cooldown duration lấy từ per-service config.

        Emergency override: nếu state cho thấy tình huống nguy hiểm,
        bỏ qua cooldown để cho phép action ngay lập tức.

        Returns: (allowed, remaining_seconds)
        """
        cfg = get_service_config(service)
        cooldown = cfg["cooldown_seconds"]

        last_time = self._last_action_time.get(service, 0)
        elapsed = time.time() - last_time
        remaining = cooldown - elapsed

        if remaining > 0:
            # Emergency override: bỏ qua cooldown nếu nguy hiểm
            if state is not None and self._is_emergency(state, cfg):
                log.warning("EMERGENCY: Bypassing cooldown for %s "
                            "(CPU=%.1f%% MEM=%.1f%% Health=%d/%d)",
                            service, state.cpu_percent, state.memory_percent,
                            state.healthy_count, state.container_count)
                return True, 0.0
            return False, remaining
        return True, 0.0

    @staticmethod
    def _is_emergency(state, cfg: dict) -> bool:
        """
        Tình huống khẩn cấp — bỏ qua cooldown:
        1. CPU > 90% (quá tải nghiêm trọng)
        2. MEM > mem_danger (sắp OOM)
        3. Containers unhealthy > 30%
        """
        # CPU critical
        if state.cpu_percent > 90:
            return True
        # Memory critical
        if state.memory_percent > cfg.get("mem_danger", 90):
            return True
        # Health critical: > 30% containers unhealthy
        if state.container_count > 0:
            health_ratio = state.healthy_count / state.container_count
            if health_ratio < 0.7:
                return True
        return False

    def record_action(self, service: str, action_name: str):
        """Ghi nhận đã thực hiện action (bắt đầu cooldown)"""
        if action_name != "no_op":
            self._last_action_time[service] = time.time()
            self._last_action_name[service] = action_name
            log.info("Cooldown started for %s: %s (duration=%ds)",
                     service, action_name,
                     get_service_config(service)["cooldown_seconds"])

    def get_status(self, service: str) -> str:
        """Trạng thái cooldown hiện tại"""
        allowed, remaining = self.can_act(service)
        if allowed:
            return "ready"
        last = self._last_action_name.get(service, "?")
        return f"cooldown {remaining:.0f}s (last: {last})"


class RollbackGuard:
    """
    Theo dõi tình trạng hệ thống sau khi agent can thiệp.

    Metrics nguồn cho rollback decision:
    ┌──────────────────┬──────────────────────────────────────────────┐
    │ Metric           │ Nguồn                                        │
    ├──────────────────┼──────────────────────────────────────────────┤
    │ Health ratio     │ ServiceState.healthy_count / container_count  │
    │ CPU spike        │ ServiceState.cpu_percent                      │
    │ Memory spike     │ ServiceState.memory_percent                   │
    └──────────────────┴──────────────────────────────────────────────┘

    TODO: Khi có app metrics exporter, bổ sung:
    │ Error rate (5xx) │ rate(http_requests_total{status=~"5.."}[5m]) │
    │ p95 latency      │ histogram_quantile(0.95, ...)                 │

    Thresholds đọc từ per-service config:
    - rollback_error_threshold: health drop tolerance (default 0.1 = 10%)
    - rollback_latency_factor: CPU spike multiplier (default 2.0x)
    - rollback_window_seconds: how long to monitor after action (default 300s)
    - rollback_action: what to do on trigger ("rule-based" | "disable" | "none")
    """

    def __init__(self):
        self.rollback_triggered = False
        self.rollback_reason = ""

        # Snapshot trước khi agent can thiệp
        # {service: {"time": t, "cpu": c, "mem": m, "healthy": h, "total": t}}
        self._pre_action_snapshots: dict[str, dict] = {}

    def snapshot_before_action(self, service: str, state, action_name: str):
        """
        Chụp snapshot metrics trước khi thực hiện action.
        FIX WEAK-2: Chỉ ghi snapshot nếu chưa có snapshot active trong rollback window.
        Nếu đã có snapshot (action trước chưa hết window), giữ nguyên baseline gốc.
        """
        existing = self._pre_action_snapshots.get(service)
        if existing is not None:
            cfg = get_service_config(service)
            elapsed = time.time() - existing["time"]
            if elapsed < cfg["rollback_window_seconds"]:
                log.debug("Snapshot already active for %s (%.0fs ago), keeping baseline", service, elapsed)
                return  # Giữ baseline gốc

        self._pre_action_snapshots[service] = {
            "time": time.time(),
            "cpu": state.cpu_percent,
            "mem": state.memory_percent,
            "healthy": state.healthy_count,
            "total": state.container_count,
            "action": action_name,
        }
        log.debug("Snapshot taken for %s before action (%s)", service, action_name)

    def check_post_action(self, service: str, state) -> Tuple[bool, str]:
        """
        Kiểm tra sau khi action đã thực hiện.
        Dùng per-service thresholds từ config.
        Returns: (should_rollback, reason)
        """
        snapshot = self._pre_action_snapshots.get(service)
        if snapshot is None:
            return False, ""

        cfg = get_service_config(service)
        elapsed = time.time() - snapshot["time"]

        # Chờ đủ thời gian để action có hiệu lực
        # scale_up cần: ~15s start container + ~30s warm up + rate[1m] delay
        min_observation = max(90, cfg.get("rollback_min_observation", 90))
        if elapsed < min_observation:
            return False, ""

        # Hết cửa sổ rollback → clear snapshot, mọi thứ OK
        if elapsed > cfg["rollback_window_seconds"]:
            del self._pre_action_snapshots[service]
            log.debug("Rollback window expired for %s, all clear", service)
            return False, ""

        # === CHECK 1: Health ratio giảm QUÁ NGHIÊM TRỌNG ===
        # Khi bị Delay/Error Rate, Probe sẽ fail và Container bị coi là unhealthy.
        # Đó là MÔI TRƯỜNG BÌNH THƯỜNG lúc có Chaos. Không được quy kết là do RL làm hỏng
        # Trừ khi Health Ratio rớt THÊM > 50% so với lúc mới nhận lệnh.
        health_before = snapshot["healthy"] / max(snapshot["total"], 1)
        health_now = state.healthy_count / max(state.container_count, 1)

        # Ngưỡng chịu đựng nới lỏng (vd cho phép health rớt thêm 40% mà ko rollback)
        threshold = max(cfg.get("rollback_error_threshold", 0.1), 0.4)

        # ĐẶC XÁ: Không quy kết trách nhiệm cho RL nếu hành động vừa làm là Cứu Hộ
        # (Vì lệnh Restart/Replace CHẮC CHẮN làm giảm Healthy Count tạm thời)
        action_name = snapshot.get("action", "")
        recovery_actions = ("restart_unhealthy", "kill_and_replace", "scale_up")

        if action_name not in recovery_actions and health_now < health_before - threshold:
            reason = (
                f"Health dropped critically after {action_name}: {health_before:.0%} → {health_now:.0%} "
                f"(threshold: -{threshold:.0%})"
            )
            return True, reason

        # === CHECK 2: CPU TỆ HƠN SAU action ===
        # Phải chờ CPU ổn định tĩnh lại. Nếu action sinh process mới, CPU tất nhiên vọt nhẹ.
        cpu_before = snapshot["cpu"]
        cpu_now = state.cpu_percent
        if action_name not in recovery_actions and cpu_now > 95 and cpu_now > cpu_before + 15:
            reason = (
                f"CPU worsened critically after {action_name}: {cpu_before:.1f}% → {cpu_now:.1f}% "
                f"(still critical after {elapsed:.0f}s)"
            )
            return True, reason

        # === CHECK 3: Memory vượt ngưỡng danger ===
        if state.memory_percent > cfg["mem_danger"]:
            # Chỉ quy kết trách nhiệm cho RL nếu hành động KHÔNG phải là Cứu Hộ,
            # HOẶC đó là hành động Cứu Hộ nhưng sau elapsed time (vd 60s) vẫn chưa hết Leak.
            if action_name not in recovery_actions or elapsed > 60:
                if snapshot["mem"] < cfg["mem_warning"] or elapsed > 60:
                    reason = (
                        f"Memory spiked/stuck: {snapshot['mem']:.1f}% → {state.memory_percent:.1f}% "
                        f"(crossed danger={cfg['mem_danger']}% after {elapsed:.0f}s)"
                    )
                    return True, reason

        # TODO: CHECK 4 - Error rate (khi có app metrics)
        # error_rate = monitor.query('rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])')
        # if error_rate > cfg.get("rollback_error_rate_threshold", 0.05): ...

        # TODO: CHECK 5 - p95 latency (khi có app metrics)
        # p95 = monitor.query('histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))')
        # if p95 > baseline_p95 * cfg["rollback_latency_factor"]: ...

        return False, ""

    def trigger_rollback(self, service: str, reason: str):
        """Đánh dấu rollback đã triggered"""
        cfg = get_service_config(service)
        self.rollback_triggered = True
        self.rollback_reason = reason
        log.warning("ROLLBACK TRIGGERED for %s: %s → action=%s",
                     service, reason, cfg["rollback_action"])

    def get_rollback_action(self, service: str) -> str:
        """Lấy rollback action cho service cụ thể"""
        cfg = get_service_config(service)
        return cfg["rollback_action"]

    def reset(self):
        """Reset rollback state"""
        self.rollback_triggered = False
        self.rollback_reason = ""
        self._pre_action_snapshots.clear()
        log.info("Rollback guard reset")


class DryRunLogger:
    """
    Shadow/Dry-run mode: log tất cả action đề xuất mà KHÔNG thực thi.
    Output JSON log để review trước khi bật live.
    """

    def __init__(self):
        self.suggestions: list[dict] = []

    def log_suggestion(
        self,
        service: str,
        action: int,
        action_name: str,
        state_dict: dict,
        decision_source: str = "rl",
    ):
        """Ghi 1 action đề xuất"""
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "service": service,
            "suggested_action": action_name,
            "action_id": action,
            "decision_source": decision_source,
            "state": state_dict,
            "executed": False,
        }
        self.suggestions.append(entry)

        log.info("[DRY-RUN] %s → %s (%s) | CPU=%.1f%% MEM=%.1f%%",
                 service, action_name, decision_source,
                 state_dict.get("cpu", 0), state_dict.get("mem", 0))
        return entry

    def export_json(self) -> list:
        return self.suggestions

    def save(self, filepath: str):
        import json, os
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(self.suggestions, f, indent=2)
        log.info("Dry-run log saved: %s (%d suggestions)", filepath, len(self.suggestions))
