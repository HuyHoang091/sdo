"""
SDO Live Agent v1.0 - Tích hợp Guardrails, Dry-run, Metrics, Rollback.

Modes:
  - live:    Thực thi action lên Docker1
  - dry-run: Chỉ log đề xuất, KHÔNG chạm Docker
  - shadow:  Alias của dry-run

Fixes applied:
- WEAK-1: Fallback counter chỉ đếm khi action thực sự dùng rules (không đếm khi cooldown)
- WEAK-2: Snapshot chỉ ghi khi chưa có snapshot active (không overwrite)
- WEAK-4: Auto re-enable RL sau rollback_cooloff_seconds
- WEAK-6: Action log flush periodic (mỗi 100 steps)
"""

import time
import json
import os
import sys
import logging
from datetime import datetime
from typing import Optional

from orchestrator.config import (
    AGENT_INTERVAL, ACTIONS, MODEL_PATH, AGENT_MODE, AGENT_METRICS_PORT,
    ONNX_MODEL_PATH, get_service_config,
)
from orchestrator.guardrails import CooldownManager, RollbackGuard, DryRunLogger
from orchestrator.agent_metrics import get_metrics, start_metrics_server
from orchestrator.ui.webhooks import send_event, has_webhooks

log = logging.getLogger("sdo.agent")

# Flush action_log mỗi N steps để tránh memory leak (WEAK-6)
# Đã đổi thành 1 để theo dõi realtime trên Dashboard
LOG_FLUSH_INTERVAL = 1

os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

class LiveAgent:
    """
    Agent v1.0 with safety guardrails:
    1. Query Prometheus → lấy state
    2. RL model predict → chọn action
    3. Cooldown check → cho phép hay chặn?
    4. Dry-run check → log hay thực thi?
    5. Thực thi action (nếu live mode)
    6. Rollback check → tình trạng xấu đi?
    7. Export metrics → Prometheus
    """

    def __init__(
        self,
        monitor,
        docker_ctl,
        service: str,
        model_path: Optional[str] = None,
        interval: int = AGENT_INTERVAL,
        use_rl: bool = True,
        mode: Optional[str] = None,
        enable_metrics: bool = True,
    ):
        self.monitor = monitor
        self.docker_ctl = docker_ctl
        self.service = service
        self.interval = interval
        self.use_rl = use_rl
        self.mode = mode or AGENT_MODE
        self.model = None
        self.action_log = []
        self.step_count = 0
        self._original_mode = self.mode  # Lưu mode gốc để restore sau rollback

        # Safety modules
        self.cooldown = CooldownManager()
        self.rollback = RollbackGuard()
        self.dryrun_logger = DryRunLogger()
        self.metrics = get_metrics()  # Singleton

        # Rollback state
        self._rl_disabled_by_rollback = False
        self._rl_disabled_at: Optional[float] = None  # Timestamp khi bị disable

        # FIX WEAK-4: Auto re-enable RL sau N giây (0 = disabled)
        self.rollback_cooloff_seconds = int(os.getenv("SDO_ROLLBACK_COOLOFF", "300"))

        # Load model: ONNX (lightweight) → SB3 (full) → rule-based
        self._model_type = None  # "onnx" | "sb3" | None
        if use_rl:
            self._load_model(model_path)

        if not self.use_rl:
            log.info("Using rule-based decision making")

        # Start metrics server (singleton, safe to call multiple times)
        if enable_metrics:
            start_metrics_server(self.metrics, AGENT_METRICS_PORT)

    def _load_model(self, model_path: str = None):
        """
        Auto-detect model format:
        1. ONNX (.onnx) — preferred, no PyTorch needed
        2. SB3 (.zip) — needs PyTorch + stable-baselines3
        3. None → fallback to rule-based
        """
        import os

        # Priority 1: ONNX (lightweight, preferred for Docker)
        onnx_path = ONNX_MODEL_PATH
        if os.path.exists(onnx_path):
            from orchestrator.inference import OnnxPredictor
            self.model = OnnxPredictor(onnx_path)
            self._model_type = "onnx"
            log.info("Loaded ONNX model from %s (lightweight mode)", onnx_path)
            return

        # Priority 2: SB3 .zip (needs PyTorch)
        mp = model_path or MODEL_PATH
        if os.path.exists(f"{mp}.zip"):
            try:
                from stable_baselines3 import PPO
                self.model = PPO.load(mp)
                self._model_type = "sb3"
                log.info("Loaded SB3 model from %s", mp)
                return
            except ImportError:
                log.warning("SB3 model found but PyTorch/SB3 not installed. "
                            "Export to ONNX: python -m orchestrator.export_onnx")

        # Fallback: rule-based
        log.warning("No model found, falling back to rule-based")
        self.use_rl = False

    @property
    def is_dry_run(self) -> bool:
        return self.mode in ("dry-run", "shadow")

    @property
    def effective_mode(self) -> str:
        if self._rl_disabled_by_rollback:
            return "rules (rollback)"
        if self.is_dry_run:
            return f"dry-run ({'RL' if self.use_rl else 'rules'})"
        return "RL (PPO)" if self.use_rl and not self._rl_disabled_by_rollback else "rule-based"

    def run(self):
        """Main loop"""
        cfg = get_service_config(self.service)
        log.info("=" * 60)
        log.info("SDO Agent v1.0 Started")
        log.info("  Service:    %s", self.service)
        log.info("  Interval:   %ds", self.interval)
        log.info("  Mode:       %s", self.effective_mode)
        log.info("  Dry-run:    %s", "YES" if self.is_dry_run else "NO (live)")
        log.info("  Cooldown:   %ds", cfg["cooldown_seconds"])
        log.info("  Containers: %d-%d", cfg["min_containers"], cfg["max_containers"])
        log.info("  Rollback:   window=%ds, threshold=%.0f%%, action=%s",
                 cfg["rollback_window_seconds"], cfg["rollback_error_threshold"] * 100,
                 cfg["rollback_action"])
        log.info("  Auto re-enable RL: %ds (0=manual only)", self.rollback_cooloff_seconds)
        log.info("  Metrics:    http://localhost:%d/metrics", AGENT_METRICS_PORT)
        log.info("=" * 60)

        try:
            while True:
                self._tick()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log.info("Agent stopped after %d steps", self.step_count)
            self._flush_log(final=True)
            if self.is_dry_run:
                filepath = f"logs/dryrun_{datetime.now():%Y%m%d_%H%M%S}.json"
                self.dryrun_logger.save(filepath)

    def _tick(self):
        """1 tick: observe → decide → guard → act → monitor rollback"""
        self.step_count += 1
        cfg = get_service_config(self.service)

        try:
            # 1. Observe
            state = self.monitor.get_state(self.service)
            obs = self.monitor.to_vector(state)

            # 2. FIX WEAK-4: Check auto re-enable RL (needs state to check stability)
            self._check_auto_reenable(state)

            # 2. Decide
            use_rl_now = self.use_rl and self.model is not None and not self._rl_disabled_by_rollback
            if use_rl_now:
                if self._model_type == "onnx":
                    action = self.model.predict(obs)
                else:
                    action, _ = self.model.predict(obs, deterministic=True)
                    action = int(action)
                decision_source = "rl"
            else:
                action = self._rule_based_action(state)
                decision_source = "rules"
                if self.use_rl and self._rl_disabled_by_rollback:
                    decision_source = "rules (rollback)"

            action_name = ACTIONS.get(action, "unknown")

            # --- NGĂN CHẶN LỆNH VÔ NGHĨA TỪ TRỨNG NƯỚC ---
            # Nếu RL báo scale_down nhưng đang ở min_containers, override ngay thành no_op
            # Tránh tình trạng RL bị chặn ở execute_action nhưng Guardrails vẫn cấm túc oan uổng
            if action == 2 and state.container_count <= cfg["min_containers"]:
                action = 0
                action_name = "no_op (min_limit)"
            elif action == 1 and state.container_count >= cfg["max_containers"]:
                action = 0
                action_name = "no_op (max_limit)"

            health_ratio = state.healthy_count / max(state.container_count, 1)

            # 3. Update metrics gauges
            self.metrics.update_gauges(
                service=self.service,
                mode="rl" if use_rl_now else ("disabled" if self._rl_disabled_by_rollback else "rules"),
                containers=state.container_count,
                cpu=state.cpu_percent,
                memory=state.memory_percent,
                health_ratio=health_ratio,
            )

            # 4. Cooldown check (per-service duration)
            cooldown_info = ""
            was_blocked_by_cooldown = False
            if not action_name.startswith("no_op"):
                can_act, remaining = self.cooldown.can_act(self.service, state=state)
                if not can_act:
                    self.metrics.record_cooldown_blocked(self.service)
                    cooldown_info = f" [cooldown {remaining:.0f}s]"
                    action_name = "no_op (cooldown)"
                    action = 0
                    was_blocked_by_cooldown = True

            # FIX WEAK-1: Chỉ đếm fallback khi thực sự dùng rules (không phải do cooldown)
            if decision_source == "rules (rollback)" and not was_blocked_by_cooldown:
                self.metrics.record_fallback(self.service)

            # 5. Log status
            status = "OK"
            if state.cpu_percent > cfg["cpu_high"] or state.memory_percent > cfg["mem_danger"]:
                status = "WARN"
            if state.error_rate > 0.2 or state.response_time_ms > 1500:
                status = "WARN"
            mode_tag = "[DRY] " if self.is_dry_run else ""
            log.info("[%04d] %s %sCPU=%.1f%% MEM=%.1f%% DELAY=%.0fms ERR=%.0f%% Cnt=%d H=%d/%d → %s (%s)%s",
                     self.step_count, status, mode_tag,
                     state.cpu_percent, state.memory_percent,
                     state.response_time_ms, state.error_rate * 100,
                     state.container_count, state.healthy_count, state.container_count,
                     action_name, decision_source, cooldown_info)

            # 6. Act or log
            if not action_name.startswith("no_op"):
                if self.is_dry_run:
                    self.dryrun_logger.log_suggestion(
                        service=self.service,
                        action=action,
                        action_name=action_name,
                        state_dict={
                            "cpu": round(state.cpu_percent, 2),
                            "mem": round(state.memory_percent, 2),
                            "containers": state.container_count,
                            "healthy": state.healthy_count,
                        },
                        decision_source=decision_source,
                    )
                    self.metrics.record_dryrun(self.service)
                else:
                    self.rollback.snapshot_before_action(self.service, state, action_name)
                    executed = self._execute_action(action, state)
                    if executed:
                        self.cooldown.record_action(self.service, action_name)

                self.metrics.record_action(self.service, action_name)

            if not self.is_dry_run and not self._rl_disabled_by_rollback:
                should_rollback, reason = self.rollback.check_post_action(self.service, state)
                if should_rollback:
                    self._handle_rollback(reason)

            self.action_log.append({
                "step": self.step_count,
                "time": datetime.now().isoformat(),
                "mode": self.effective_mode,
                "decision_source": decision_source,
                "service": self.service,
                "state": {
                    "cpu": round(state.cpu_percent, 2),
                    "mem": round(state.memory_percent, 2),
                    "delay_ms": round(state.response_time_ms, 2),
                    "error_rate": round(state.error_rate, 4),
                    "containers": state.container_count,
                    "healthy": state.healthy_count,
                    "host_cpu": round(state.host_cpu_available, 2),
                    "host_mem": round(state.host_mem_available, 2),
                },
                "action": action_name,
                "executed": not self.is_dry_run and action_name not in ("no_op", "no_op (cooldown)"),
            })

            if self.step_count % LOG_FLUSH_INTERVAL == 0:
                self._flush_log()

        except Exception as e:
            log.error("[%04d] Error: %s", self.step_count, e, exc_info=True)
            self.metrics.record_error(self.service)
            if has_webhooks():
                send_event(
                    "agent_error",
                    {
                        "service": self.service,
                        "time": datetime.now().isoformat(),
                        "message": str(e),
                        "state": {
                            "cpu": round(getattr(state, "cpu_percent", 0.0), 2) if "state" in locals() else 0.0,
                            "mem": round(getattr(state, "memory_percent", 0.0), 2) if "state" in locals() else 0.0,
                            "containers": getattr(state, "container_count", 0) if "state" in locals() else 0,
                            "healthy": getattr(state, "healthy_count", 0) if "state" in locals() else 0,
                            "error_rate": getattr(state, "error_rate", 0.0) if "state" in locals() else 0.0,
                            "response_time_ms": getattr(state, "response_time_ms", 0.0) if "state" in locals() else 0.0,
                        },
                        "data": {
                            "step": self.step_count,
                            "mode": self.effective_mode,
                        },
                    },
                )

    def _graceful_drain(self, action_name: str, target_container_id: Optional[str] = None, service: str = None):
        """
        Móc nối (Hook) chạy trước khi thực thi các lệnh phá hủy (scale_down, restart, kill).
        Tự động gỡ IP của container mục tiêu khỏi Nginx Load Balancer.
        """
        service = service or self.service
        if target_container_id:
            log.info("  [Hook] Chủ động tháo LB cho Container %s trước khi %s...", target_container_id[:12], action_name)
            self.docker_ctl.update_load_balancer(service, exclude_container_id=target_container_id)

        # Giai đoạn Connection Draining giả lập (SRE Pattern)
        drain_timeout = 10 # Giây
        log.warning("  [%s] [Graceful] Bắt đầu quá trình Draining (Rút mạng) %ss trước khi thực thi lệnh %s...", service, drain_timeout, action_name)

        for s in range(drain_timeout, 0, -1):
            sys.stdout.write(f"\r  [{service}][Graceful] Chờ xử lý request cũ (Draining)... {s}s ")
            sys.stdout.flush()
            time.sleep(1)
        print() # Xuống dòng

    def _execute_action(self, action: int, state, service: str = None) -> bool:
        """Thực thi action lên Docker. Trả về True nếu thực sự thực hiện."""
        service = service or self.service
        cfg = get_service_config(service)
        current = self.docker_ctl.count_containers(service)
        action_name = ACTIONS.get(action, "unknown")

        target_cid = None
        if action == 2 and current > cfg["min_containers"]: # scale_down
            # Compose kill index cao nhất, lấy container name lớn nhất (VD: -3 thay vì -1)
            infos = self.docker_ctl.get_containers_info(service)
            if infos:
                infos.sort(key=lambda x: x["name"])
                target_cid = infos[-1]["id"]
        elif action in (3, 6): # restart_unhealthy, kill_and_replace
            target_cid = self.docker_ctl._find_worst_container(service)

        # Gọi Hook rút mạng kết nối trước các lệnh phá hủy
        if action in (2, 6): # scale_down, kill_and_replace
            if action == 2 and current == cfg["min_containers"]:
                log.warning("  [%s] Already at min containers (%d)",
                            service, cfg["min_containers"])
                return False
            # === Pre-scale trước khi kill_and_replace ===
            if action == 6:
                if current < cfg["max_containers"]:
                    log.info("  [%s] Pre-scaling (Tạo mới dự phòng) trước khi kill...", service)
                    self.docker_ctl.scale_up(service)
                    log.info("  [%s] Đợi 5s cho container mới khởi động sẵn sàng...", service)
                    time.sleep(5)
                else:
                    log.warning("  [%s] Không thể pre-scale vì đã đạt max limit", service)
            self._graceful_drain(action_name, target_container_id=target_cid, service=service)

        if action == 1:  # scale_up
            # Nếu service có replica config → scale replica thay vì primary
            if cfg.get("replica_enabled") and cfg.get("replica_service"):
                replica_svc = cfg["replica_service"]
                replica_max = cfg.get("replica_max", 3)
                replica_count = self.docker_ctl.count_containers(replica_svc)
                if replica_count < replica_max:
                    log.info("  [%s] Redirecting scale_up → replica '%s'",
                             service, replica_svc)
                    self.docker_ctl.scale_up(replica_svc)
                    return True
                else:
                    log.warning("  [%s] Replica '%s' đã đạt max (%d)",
                                service, replica_svc, replica_max)
                    return False
            # Normal scale_up
            if current < cfg["max_containers"]:
                self.docker_ctl.scale_up(service)
                return True
            else:
                log.warning("  [%s] Already at max containers (%d)",
                            service, cfg["max_containers"])
                return False
        elif action == 2:  # scale_down
            if current > cfg["min_containers"]:
                self.docker_ctl.scale_down(service)
                return True
            else:
                log.warning("  [%s] Already at min containers (%d)",
                            service, cfg["min_containers"])
                return False
        elif action == 3:  # restart_unhealthy
            n = self.docker_ctl.restart_unhealthy(service)
            return n > 0
        elif action == 4:
            self.docker_ctl.increase_cpu_limit(service)
            return True
        elif action == 5:
            self.docker_ctl.increase_mem_limit(service)
            return True
        elif action == 6:
            self.docker_ctl.kill_and_replace(service)
            return True
        return False

    def _handle_rollback(self, reason: str):
        """Xử lý rollback khi phát hiện tình trạng xấu đi"""
        rollback_action = self.rollback.get_rollback_action(self.service)
        self.rollback.trigger_rollback(self.service, reason)
        self.metrics.record_rollback(self.service)

        if has_webhooks():
            send_event(
                "rollback_triggered",
                {
                    "service": self.service,
                    "time": datetime.now().isoformat(),
                    "reason": reason,
                    "rollback_action": rollback_action,
                },
            )

        if rollback_action == "rule-based":
            log.warning("Switching %s to rule-based mode", self.service)
            self._rl_disabled_by_rollback = True
            self._rl_disabled_at = time.time()
        elif rollback_action == "disable":
            log.warning("Disabling agent for %s (log-only mode)", self.service)
            self.mode = "dry-run"
            self._rl_disabled_by_rollback = True
            self._rl_disabled_at = time.time()
        else:
            log.warning("Rollback noted for %s, continuing (action=none)", self.service)

    def _check_auto_reenable(self, state):
        """
        FIX WEAK-4: Tự bật lại RL nếu đã qua rollback_cooloff_seconds
        VÀ hệ thống đã chứng minh được sự ỔN ĐỊNH trở lại.
        """
        if not self._rl_disabled_by_rollback or self._rl_disabled_at is None:
            return
        if self.rollback_cooloff_seconds <= 0:
            return  # 0 = manual only

        elapsed = time.time() - self._rl_disabled_at
        if elapsed >= self.rollback_cooloff_seconds:
            cfg = get_service_config(self.service)
            # Hệ thống được coi là ổn định khi:
            is_stable = (
                state.cpu_percent < cfg["cpu_high"] and
                state.memory_percent < cfg.get("mem_warning", 80) and
                state.error_rate < 0.1 and
                state.response_time_ms < 1000 and
                state.healthy_count == state.container_count
            )

            if is_stable:
                log.info("System is STABLE. Auto re-enabling RL for %s after %.0fs cooloff",
                         self.service, elapsed)
                self.re_enable_rl()
            else:
                log.debug("Cooloff passed but %s is still unstable. Waiting to re-enable RL.", self.service)

    def _rule_based_action(self, state, service: str = None) -> int:
        """
        Fallback HPA-style: Quyết định dựa trên thuật toán K8s HPA và Liveness Probes.
        (Được gọi khi RL Agent bị lỗi hoặc rollback)
        """
        import math
        service = service or self.service
        cfg = get_service_config(service)

        # 1. Kubelet Liveness/Readiness Probe behavior (Crash & Fatal errors)
        is_starting_phase = (state.healthy_count < state.container_count) and (state.cpu_percent < 5.0) and (state.error_rate < 0.1)

        if state.healthy_count < state.container_count and not is_starting_phase:
            return 3  # restart_unhealthy
        if state.error_rate > 0.5:
            return 6  # kill_and_replace
        elif state.error_rate > 0.3:
            return 3  # restart_unhealthy
            
        # 2. OOMKiller behavior
        if state.memory_percent > cfg["mem_danger"]:
            return 6

        # 3. K8s HPA (Horizontal Pod Autoscaler) Core Algorithm
        # Công thức: desiredReplicas = ceil[currentReplicas * ( currentMetricValue / desiredMetricValue )]
        target_cpu = cfg.get("cpu_high", 80.0)
        current_cpu = max(0.1, state.cpu_percent)
        current_replicas = state.container_count
        
        ratio = current_cpu / target_cpu
        
        # HPA Native Tolerance (cố định ở mức 10%, tức ratio không thuộc [0.9, 1.1])
        if ratio > 1.1 or ratio < 0.9:
            desired_replicas = math.ceil(current_replicas * ratio)
            
            if desired_replicas > current_replicas:
                # Cần thêm replicas -> Gọi lệnh scale_up
                return 1 
            elif desired_replicas < current_replicas:
                # Cần giảm replicas -> Ràng buộc thêm response time để chắc chắn hệ thống rảnh rỗi (Custom Guard)
                if state.response_time_ms < 500:
                    return 2  # scale_down

        # 4. Custom SDO Edge Cases (K8s HPA vốn không rành mấy cái này, ta giữ làm fallback phụ)
        if state.response_time_ms > 1500:
            return 1  # scale_up delay
        if state.disk_io_percent > 85:
            return 6  # kill_and_replace
            
        return 0

    def re_enable_rl(self):
        """Bật lại RL mode sau rollback (gọi thủ công hoặc auto)"""
        self._rl_disabled_by_rollback = False
        self._rl_disabled_at = None
        self.rollback.reset()
        # FIX BUG #1: Reset mode về gốc (khỏi dry-run do rollback_action="disable")
        if self.mode != self._original_mode:
            log.info("Restoring mode: %s → %s", self.mode, self._original_mode)
            self.mode = self._original_mode
        log.info("RL re-enabled for %s", self.service)

    def _flush_log(self, final: bool = False):
        """
        Ghi log realtime để Dashboard UI đọc được ngay.
        Dùng chung 1 file log cho mỗi ngày để dễ theo dõi.
        """
        if not self.action_log:
            return
        os.makedirs("logs", exist_ok=True)
        date_tag = datetime.now().strftime("%Y%m%d")
        filepath = f"logs/agent_log_{date_tag}.json"
        
        # Load Existing
        existing_logs = []
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    existing_logs = json.load(f)
            except: pass
            
        # Append New
        existing_logs.extend(self.action_log)
        
        # Cắt bớt nếu file dài quá (giữ 5000 records cuối cùng)
        if len(existing_logs) > 5000:
            existing_logs = existing_logs[-5000:]
            
        with open(filepath, "w") as f:
            json.dump(existing_logs, f, indent=2)
            
        if final:
            log.info("Final action log flushed to %s", filepath)
            
        self.action_log.clear()

# =============================================
# MULTI-SERVICE AGENT (v2.0 upgrade)
# =============================================

class MultiServiceAgent(LiveAgent):
    """
    Agent quản lý NHIỀU services theo priority.

    Kiến trúc:
    - CÙNG 1 RL model, predict per-service với 28D obs
    - Bottleneck filter layer chặn action vô nghĩa
    - Parallel probe, sequential execute (topology order)
    - UnifiedRewardFunction shared với training env
    """

    def __init__(
        self,
        monitor,
        docker_ctl,
        services: list,
        model_path: str = None,
        interval: int = None,
        use_rl: bool = True,
        mode: str = None,
        enable_metrics: bool = True,
    ):
        from orchestrator.config import (
            MULTI_AGENT_INTERVAL, AGENT_MODE, AGENT_METRICS_PORT,
            MULTI_MODEL_PATH, MULTI_ONNX_PATH, SERVICE_CONFIGS,
        )
        from orchestrator.topology import ServiceTopology
        from orchestrator.reward import UnifiedRewardFunction

        self.monitor = monitor
        self.docker_ctl = docker_ctl
        self.services = services
        self.interval = interval or MULTI_AGENT_INTERVAL
        self.use_rl = use_rl
        self.mode = mode or AGENT_MODE
        self.step_count = 0
        self.action_log = []
        self._original_mode = self.mode

        # Topology
        # Chỉ lấy config cho services được quản lý
        managed_configs = {s: SERVICE_CONFIGS[s] for s in services if s in SERVICE_CONFIGS}
        self.topology = ServiceTopology(managed_configs)

        # Unified reward
        self.reward_fn = UnifiedRewardFunction(self.topology)

        # Per-service guardrails
        self.cooldowns = {svc: CooldownManager() for svc in services}
        self.rollbacks = {svc: RollbackGuard() for svc in services}

        # Shared model (28D input)
        self.model = None
        self._model_type = None
        if use_rl:
            self._load_multi_model(model_path)
        if not self.use_rl:
            log.info("MultiServiceAgent: using rule-based mode")

        # Shared metrics
        self.metrics = get_metrics()
        if enable_metrics:
            start_metrics_server(self.metrics, AGENT_METRICS_PORT)

        # Rollback state per-service
        self._rl_disabled = {svc: False for svc in services}
        self._rl_disabled_at = {svc: None for svc in services}
        self.rollback_cooloff_seconds = int(os.getenv("SDO_ROLLBACK_COOLOFF", "300"))

    # def _load_multi_model(self, model_path: str = None):
    #     """Load model cho 28D obs. Ưu tiên ONNX → SB3 → rule-based."""
    #     from orchestrator.config import MULTI_ONNX_PATH, MULTI_MODEL_PATH

    #     onnx_path = MULTI_ONNX_PATH
    #     if os.path.exists(onnx_path):
    #         from orchestrator.inference import OnnxPredictor
    #         self.model = OnnxPredictor(onnx_path)
    #         self._model_type = "onnx"
    #         log.info("Loaded multi-service ONNX model: %s", onnx_path)
    #         return

    #     mp = model_path or MULTI_MODEL_PATH
    #     if os.path.exists(f"{mp}.zip"):
    #         try:
    #             from stable_baselines3 import PPO
    #             self.model = PPO.load(mp)
    #             self._model_type = "sb3"
    #             log.info("Loaded multi-service SB3 model: %s", mp)
    #             return
    #         except ImportError:
    #             log.warning("SB3 not installed, falling back to rule-based")

    #     log.warning("No multi-service model found, using rule-based")
    #     self.use_rl = False

    def _load_multi_model(self, model_path: str = None):
        """Load multi-service model (BC hoặc SB3 fallback)."""
        from orchestrator.config import MULTI_MODEL_PATH
        mp = MULTI_MODEL_PATH

        # Ưu tiên BC model (.pt)
        if os.path.exists(f"{mp}.pt"):
            from orchestrator.bc_model import BCModel
            self.model = BCModel.load(mp)
            self._model_type = "bc"
            log.info("Loaded BC model: %s", mp)
            return

        # Fallback: SB3
        if os.path.exists(f"{mp}.zip"):
            try:
                from stable_baselines3 import PPO
                self.model = PPO.load(mp)
                self._model_type = "sb3"
                log.info("Loaded SB3 model: %s", mp)
                return
            except ImportError:
                pass

        # Fallback: ONNX
        onnx_path = os.environ.get("ONNX_MODEL_PATH", mp + ".onnx")
        if os.path.exists(onnx_path):
            from orchestrator.onnx_runtime import ONNXModel
            self.model = ONNXModel(onnx_path)
            self._model_type = "onnx"
            log.info("Loaded ONNX model: %s", onnx_path)
            return

        log.warning("No multi-service model found, using rule-based")
        self.use_rl = False

    # @property
    # def is_dry_run(self) -> bool:
    #     return self.mode in ("dry-run", "shadow")

    def run(self):
        """Main loop."""
        log.info("=" * 60)
        log.info("SDO Multi-Service Agent Started")
        log.info("  Services:  %s", self.services)
        log.info("  Order:     %s", self.topology.get_processing_order())
        log.info("  Interval:  %ds", self.interval)
        log.info("  Mode:      %s", "RL" if self.use_rl else "rule-based")
        log.info("  Dry-run:   %s", "YES" if self.is_dry_run else "NO")
        self.topology.print_topology()
        log.info("=" * 60)

        try:
            while True:
                self._tick()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log.info("Multi-agent stopped after %d steps", self.step_count)
            self._flush_log(final=True)

    def _tick(self):
        """1 tick: parallel probe → bottleneck detect → ordered decision → sequential execute."""
        self.step_count += 1

        try:
            # === Phase 0: Host Overload Circuit Breaker ===
            import psutil
            from orchestrator.config import (
                HOST_CPU_CRITICAL, HOST_MEM_CRITICAL,
                CIRCUIT_BREAKER_FRONTEND, CIRCUIT_BREAKER_RECOVERY_THRESHOLD,
            )
            host_cpu = psutil.cpu_percent(interval=0.5)
            host_mem = psutil.virtual_memory().percent

            if host_cpu > HOST_CPU_CRITICAL or host_mem > HOST_MEM_CRITICAL:
                if not getattr(self, '_circuit_breaker_active', False):
                    self._circuit_breaker_active = True
                    log.critical(
                        "🔴 CIRCUIT BREAKER ON: Host CPU=%.0f%% MEM=%.0f%% "
                        "— Gỡ mạng tất cả trừ %s",
                        host_cpu, host_mem, CIRCUIT_BREAKER_FRONTEND)
                    for svc in self.services:
                        if svc == CIRCUIT_BREAKER_FRONTEND:
                            continue
                        for cid in self.docker_ctl.get_container_ids(svc):
                            self.docker_ctl.update_load_balancer(
                                svc, exclude_container_id=cid)
                        log.warning("  [%s] Đã gỡ khỏi LB (circuit breaker)", svc)

                log.warning("[%04d] 🔴 CIRCUIT BREAKER ACTIVE — Host CPU=%.0f%% MEM=%.0f%% — Chờ recovery...",
                            self.step_count, host_cpu, host_mem)
                time.sleep(self.interval)
                return

            # Recovery: host tụt về dưới ngưỡng → bật lại
            if getattr(self, '_circuit_breaker_active', False):
                if host_cpu < CIRCUIT_BREAKER_RECOVERY_THRESHOLD and host_mem < CIRCUIT_BREAKER_RECOVERY_THRESHOLD:
                    self._circuit_breaker_active = False
                    log.critical(
                        "🟢 CIRCUIT BREAKER OFF: Host CPU=%.0f%% MEM=%.0f%% "
                        "— Khôi phục mạng, tiếp tục hoạt động",
                        host_cpu, host_mem)
                    for svc in self.services:
                        self.docker_ctl.update_load_balancer(svc)
                        log.info("  [%s] Đã khôi phục LB", svc)
                else:
                    log.warning("[%04d] 🟡 CIRCUIT BREAKER — Đang recovery Host CPU=%.0f%% MEM=%.0f%%",
                                self.step_count, host_cpu, host_mem)
                    time.sleep(self.interval)
                    return

            # Block scale_up khi host đang căng (chưa tới critical nhưng > 80%)
            self._host_stressed = (host_cpu > 95 or host_mem > 90)

            # === Phase 1: Parallel Probe ===
            all_states = self.monitor.get_multi_state(self.services)

            # === Phase 1.5: Auto re-enable RL per-service ===
            for svc in self.services:
                if self._rl_disabled.get(svc) and self._rl_disabled_at.get(svc):
                    elapsed = time.time() - self._rl_disabled_at[svc]
                    if elapsed >= self.rollback_cooloff_seconds:
                        state = all_states.get(svc)
                        if state and self._is_stable(state, svc):
                            self._rl_disabled[svc] = False
                            self._rl_disabled_at[svc] = None
                            self.rollbacks[svc].reset()
                            log.info("Auto re-enable RL for %s after %.0fs cooloff", svc, elapsed)

            # === Phase 2: Bottleneck Detection (1 lần, cached) ===
            self.topology.record_tick(all_states)
            bottleneck = self.topology.detect_bottleneck()

            if bottleneck:
                log.warning("[%04d] 🚨 BOTTLENECK: %s (tier %d)",
                            self.step_count, bottleneck, self.topology.get_tier(bottleneck))

            # === Phase 3: Per-service Decision (topology order) ===
            order = self.topology.get_processing_order()
            action_plan = []

            for svc in order:
                state = all_states.get(svc)
                if state is None:
                    continue

                cfg = get_service_config(svc)

                # Build 28D obs
                obs = self.monitor.to_multi_vector(svc, state, all_states, self.topology)

                # Decide
                use_rl_now = (
                    self.use_rl and self.model is not None
                    and not self._rl_disabled.get(svc, False)
                )
                if use_rl_now:
                    if self._model_type == "onnx":
                        raw_action = self.model.predict(obs)
                    else:
                        raw_action, _ = self.model.predict(obs, deterministic=True)
                        raw_action = int(raw_action)
                    source = "rl"
                else:
                    raw_action = self._rule_based_action(state, svc)
                    source = "rules"

                action_name = ACTIONS.get(raw_action, "unknown")

                # === [1] Non-scalable service: redirect scale → resource limit ===
                # Phải chạy TRƯỚC limit checks để action 1→4, 6→5 không bị chặn
                if not cfg.get("scalable", True):
                    if raw_action == 1:
                        raw_action, action_name = 4, "increase_cpu_limit"
                        log.info("  [%s] scale_up → increase_cpu_limit (non-scalable)", svc)
                    elif raw_action == 2:
                        raw_action, action_name = 0, "no_op (non-scalable)"
                    elif raw_action == 6:
                        raw_action, action_name = 5, "increase_mem_limit"
                        log.info("  [%s] kill_and_replace → increase_mem_limit (non-scalable)", svc)

                # === [2] Tier-1 protection: CẤM action phá hủy cho DB ===
                tier = self.topology.get_tier(svc)
                if tier == 1 and raw_action in (6,):
                    log.warning("  [%s] BLOCKED %s: Cấm tuyệt đối cho Tier-1 (DB)",
                                svc, action_name)
                    raw_action, action_name = 0, "no_op (tier1_protected)"

                # === [3] Limit checks ===
                if raw_action == 2 and state.container_count <= cfg["min_containers"]:
                    raw_action, action_name = 0, "no_op (min_limit)"
                elif raw_action == 1 and state.container_count >= cfg["max_containers"]:
                    raw_action, action_name = 0, "no_op (max_limit)"
                elif raw_action == 6 and cfg["mem_warning"] <= state.memory_percent <= cfg["mem_danger"]:
                    raw_action, action_name = 5, "increase_mem_limit (mem_warning)"
                elif raw_action == 6 and state.memory_percent <= cfg["mem_danger"]:
                    raw_action, action_name = 0, "no_op (mem_safe)"

                # === [4] Cấm scale_up khi host quá tải ===
                if raw_action == 1 and getattr(self, '_host_stressed', False):
                    raw_action, action_name = 0, "no_op (host_stressed)"

                # === Bottleneck Filter ===
                raw_action, action_name, source = self._apply_bottleneck_filter(
                    svc, raw_action, action_name, source, bottleneck, all_states, state
                )

                # Cooldown check
                if not action_name.startswith("no_op"):
                    can_act, remaining = self.cooldowns[svc].can_act(svc, state=state)
                    if not can_act:
                        # Bottleneck bypass: cho bottleneck service bypass cooldown
                        if svc == bottleneck:
                            log.warning("  [%s] Bypassing cooldown for BOTTLENECK", svc)
                        else:
                            raw_action, action_name = 0, "no_op (cooldown)"

                action_plan.append((svc, raw_action, action_name, source, state))

                # Log
                health_ratio = state.healthy_count / max(state.container_count, 1)
                tier = self.topology.get_tier(svc)
                mode_tag = "[DRY] " if self.is_dry_run else ""
                bn_tag = " 🚨BN" if svc == bottleneck else ""
                ds_tag = " ⬇️DS" if self.topology.is_downstream_of_bottleneck(svc) else ""
                log.info(
                    "[%04d] %s[T%d] %sCPU=%.0f%% MEM=%.0f%% DL=%.0fms ERR=%.0f%% H=%d/%d → %s (%s)%s%s",
                    self.step_count, mode_tag, tier, svc + " ",
                    state.cpu_percent, state.memory_percent,
                    state.response_time_ms, state.error_rate * 100,
                    state.healthy_count, state.container_count,
                    action_name, source, bn_tag, ds_tag,
                )

            # === Phase 4: Sequential Execute (topology order) ===
            for svc, action, action_name, source, state in action_plan:
                if action_name.startswith("no_op"):
                    continue
                if self.is_dry_run:
                    self.metrics.record_dryrun(svc)
                else:
                    self.rollbacks[svc].snapshot_before_action(svc, state, action_name)
                    executed = self._execute_action(action, state, svc)
                    if executed:
                        self.cooldowns[svc].record_action(svc, action_name)

                self.metrics.record_action(svc, action_name)

            # === Phase 4.5: Auto-recovery resource cho non-scalable services ===
            if not self.is_dry_run:
                for svc in order:
                    cfg = get_service_config(svc)
                    if cfg.get("scalable", True):
                        continue
                    state = all_states.get(svc)
                    if state is None:
                        continue
                    if (state.cpu_percent < cfg["cpu_low"]
                        and state.memory_percent < cfg["mem_warning"]
                        and state.error_rate < 0.05):
                        can_act, _ = self.cooldowns[svc].can_act(svc, state=state)
                        if can_act:
                            base = self.docker_ctl.get_base_limits(svc)
                            decreased = False
                            if self.docker_ctl.decrease_cpu_limit(svc, min_cpus=base["cpus"]):
                                decreased = True
                            if self.docker_ctl.decrease_mem_limit(svc, min_mb=base["memory_mb"]):
                                decreased = True
                            if decreased:
                                self.cooldowns[svc].record_action(svc, "resource_recovery")
                                log.info("  [%s] Auto-recovery: giảm resource → base (cpu=%.2f, mem=%dMB)",
                                         svc, base["cpus"], base["memory_mb"])

            # === Phase 5: Rollback check ===
            if not self.is_dry_run:
                for svc in order:
                    state = all_states.get(svc)
                    if state and not self._rl_disabled.get(svc, False):
                        should_rb, reason = self.rollbacks[svc].check_post_action(svc, state)
                        if should_rb:
                            self._handle_rollback(svc, reason)

            # Append log
            for svc, action, action_name, source, state in action_plan:
                reward = self.reward_fn.calculate(
                    svc, action, state, all_states,
                    self.topology, bottleneck=bottleneck,
                )
                self.action_log.append({
                    "step": self.step_count, "time": datetime.now().isoformat(),
                    "service": svc, "tier": self.topology.get_tier(svc),
                    "action": action_name, "decision_source": source,
                    "bottleneck": bottleneck,
                    "reward": round(reward, 4),
                    "state": {
                        "cpu": round(state.cpu_percent, 2),
                        "mem": round(state.memory_percent, 2),
                        "containers": state.container_count,
                        "healthy": state.healthy_count,
                        "error_rate": round(state.error_rate, 4),
                    },
                })

            if self.step_count % LOG_FLUSH_INTERVAL == 0:
                self._flush_log()

        except Exception as e:
            log.error("[%04d] MultiAgent error: %s", self.step_count, e, exc_info=True)

    def _is_stable(self, state, service: str) -> bool:
        """Check service đã ổn định chưa (dùng cho auto re-enable RL)."""
        cfg = get_service_config(service)
        return (
            state.cpu_percent < cfg["cpu_high"]
            and state.memory_percent < cfg.get("mem_warning", 80)
            and state.error_rate < 0.1
            and state.response_time_ms < 1000
            and state.healthy_count == state.container_count
        )

    def _apply_bottleneck_filter(self, svc, action, action_name, source,
                                  bottleneck, all_states, state):
        """
        Bottleneck filter layer — chặn action vô nghĩa.
        Rule 1: downstream of bottleneck → chặn scale_up
        Rule 2: service IS bottleneck → ưu tiên (handled ở cooldown bypass)
        Rule 3: upstream recovering → chặn scale_down
        """
        if bottleneck and self.topology.is_downstream_of_bottleneck(svc):
            if action == 1:  # scale_up downstream khi bottleneck
                log.info("  [%s] BLOCKED scale_up: downstream of bottleneck %s", svc, bottleneck)
                return 0, "no_op (downstream_blocked)", source + "+filter"

        if action == 2:  # scale_down
            if self.topology.is_upstream_recovering(svc, all_states):
                log.info("  [%s] BLOCKED scale_down: upstream recovering", svc)
                return 0, "no_op (upstream_recovering)", source + "+filter"

        return action, action_name, source

    # def _rule_based_action(self, state, service: str) -> int:
    #     """Rule-based decision (giống LiveAgent nhưng per-service config)."""
    #     cfg = get_service_config(service)
    #     if state.healthy_count < state.container_count:
    #         return 3
    #     if state.error_rate > 0.3:
    #         return 6 if state.error_rate > 0.5 else 3
    #     if state.response_time_ms > 1500:
    #         return 1
    #     if state.memory_percent > cfg["mem_danger"]:
    #         return 6
    #     if state.disk_io_percent > 85:
    #         return 6
    #     if state.cpu_percent > cfg["cpu_high"]:
    #         return 1
    #     if (state.cpu_percent < cfg["cpu_low"]
    #         and state.container_count > cfg["min_containers"]
    #         and state.response_time_ms < 500
    #         and state.error_rate < 0.05):
    #         return 2
    #     return 0

    def _handle_rollback(self, service: str, reason: str):
        """Handle rollback cho 1 service."""
        self.rollbacks[service].trigger_rollback(service, reason)
        self.metrics.record_rollback(service)
        self._rl_disabled[service] = True
        self._rl_disabled_at[service] = time.time()
        log.warning("Rollback for %s: %s", service, reason)

    def _flush_log(self, final: bool = False):
        """Ghi multi-service action log."""
        if not self.action_log:
            return
        os.makedirs("logs", exist_ok=True)
        date_tag = datetime.now().strftime("%Y%m%d")
        filepath = f"logs/multi_agent_log_{date_tag}.json"

        existing = []
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    existing = json.load(f)
            except:
                pass

        existing.extend(self.action_log)
        if len(existing) > 10000:
            existing = existing[-10000:]

        with open(filepath, "w") as f:
            json.dump(existing, f, indent=2)

        if final:
            log.info("Final multi-agent log: %s", filepath)
        self.action_log.clear()
