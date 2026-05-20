"""
Service Agent - Hierarchical orchestration support for multi-agent RL architecture.

This module defines the ServiceAgent class that extends LiveAgent with the ability
to receive and process orchestration commands from a Core Agent, enabling
hierarchical multi-agent coordination.
"""

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from orchestrator.agent import LiveAgent
from orchestrator.commands import CommandResult, CommandType, OrchestrationCommand
from orchestrator.config import ACTIONS, get_service_config

if TYPE_CHECKING:
    from orchestrator.core_agent import CoreAgent

log = logging.getLogger(__name__)


class ServiceAgent(LiveAgent):
    """
    Service Agent extends LiveAgent with hierarchical orchestration support.

    Key differences from LiveAgent:
    1. Can receive Orchestration_Commands from Core Agent
    2. Merges Core Agent recommendations with local RL predictions
    3. Reports execution status back to Core Agent
    4. Can operate in standalone mode when Core Agent is unavailable
    """

    def __init__(
        self,
        monitor,
        docker_ctl,
        service: str,
        model_path: Optional[str] = None,
        interval: int = 10,
        use_rl: bool = True,
        mode: Optional[str] = None,
        enable_metrics: bool = True,
        core_agent: Optional["CoreAgent"] = None,
    ) -> None:
        """
        Initialize Service Agent.

        Args:
            monitor: Metrics collection interface
            docker_ctl: Docker control interface
            service: Service name this agent manages
            model_path: Path to single-service RL model
            interval: Tick interval in seconds
            use_rl: Whether to use RL model
            mode: Agent mode ("live", "dry-run", "shadow")
            enable_metrics: Whether to enable Prometheus metrics
            core_agent: Reference to Core Agent (optional, for standalone mode)
        """
        # Call parent __init__ first
        super().__init__(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service=service,
            model_path=model_path,
            interval=interval,
            use_rl=use_rl,
            mode=mode,
            enable_metrics=enable_metrics,
        )

        # Hierarchical orchestration attributes
        self.core_agent_ref: Optional["CoreAgent"] = core_agent
        self.pending_command: Optional[OrchestrationCommand] = None
        self.standalone_mode: bool = core_agent is None

        # Track last command result for reporting
        self._last_result: Optional[CommandResult] = None

        # Failover and reconnection tracking (Requirements: 14.1)
        self._core_agent_unavailable_count: int = 0
        self._reconnection_attempt_interval: int = 30  # seconds
        self._last_reconnection_attempt: float = 0.0

    def receive_command(self, cmd: OrchestrationCommand) -> None:
        """
        Receive and queue command from Core Agent.

        Validates command structure and stores for next tick processing.
        If the command is invalid, logs a warning and ignores it.

        Args:
            cmd: Orchestration command to process

        Requirements: 5.1
        """
        if not self._validate_command(cmd):
            return
        self.pending_command = cmd
        log.debug( # Đổi INFO thành DEBUG cho đỡ rác log
            "[%s] Received command from Core Agent: type=%s, bottleneck=%s, recommended_action=%s",
            self.service,
            cmd.command_type.value,
            cmd.bottleneck_service,
            cmd.recommended_action,
        )

    def _validate_command(self, cmd: OrchestrationCommand) -> bool:
        """
        Validate command structure and parameters.

        Checks that:
        1. Command passes basic validation (target_service, action range)
        2. Target service matches this agent's service
        3. Command type is valid

        Args:
            cmd: Command to validate

        Returns:
            True if command is valid, False otherwise

        Requirements: 5.2
        """
        # Use the command's built-in validation
        if not cmd.validate():
            return False

        # Verify target_service matches this agent's service
        if cmd.target_service != self.service:
            log.warning(
                "[%s] Command target_service mismatch: expected %s, got %s",
                self.service,
                self.service,
                cmd.target_service,
            )
            return False

        # Verify command_type is valid
        if not isinstance(cmd.command_type, CommandType):
            log.warning(
                "[%s] Invalid command_type: %s",
                self.service,
                cmd.command_type,
            )
            return False

        return True

    def _merge_command_with_rl(self, cmd: OrchestrationCommand, state, rl_action: int) -> int:
        """
        Merge Core Agent recommendation with local RL prediction.

        Merge strategy:
        1. If command is EMERGENCY_PRIORITY, use recommended action
        2. If command is BOTTLENECK_DETECTED and service IS bottleneck,
           prefer scale_up/resource actions
        3. If command blocks certain actions (scale_up for downstream),
           filter RL output
        4. Otherwise, use RL prediction with command as context

        Args:
            cmd: Orchestration command from Core Agent
            state: Current service state
            rl_action: Action predicted by local RL model

        Returns:
            Merged action (0-6)

        Requirements: 5.3
        """
        if cmd is None:
            return rl_action

        # Rule 1: EMERGENCY_PRIORITY - use recommended action immediately
        if cmd.command_type == CommandType.EMERGENCY_PRIORITY:
            if cmd.recommended_action is not None:
                log.info(
                    "[%s] EMERGENCY_PRIORITY: using recommended action %d",
                    self.service,
                    cmd.recommended_action,
                )
                return cmd.recommended_action

        # Rule 2: BOTTLENECK_DETECTED - this service is the bottleneck
        if cmd.command_type == CommandType.BOTTLENECK_DETECTED:
            if cmd.bottleneck_service == self.service:
                # This service IS the bottleneck - prefer scale_up or resource actions
                cfg = get_service_config(self.service)
                # If RL suggests scale_up or resource increase, keep it
                if rl_action in (1, 4, 5):  # scale_up, increase_cpu, increase_mem
                    log.info(
                        "[%s] BOTTLENECK_DETECTED (self): keeping RL action %d",
                        self.service,
                        rl_action,
                    )
                    return rl_action
                # If recommended action is scale_up, use it
                if cmd.recommended_action == 1:
                    log.info(
                        "[%s] BOTTLENECK_DETECTED (self): using recommended scale_up",
                        self.service,
                    )
                    return 1
            else:
                # This service is DOWNSTREAM of bottleneck
                # Block scale_up - it won't help and wastes resources
                if rl_action == 1:  # scale_up
                    log.info(
                        "[%s] BOTTLENECK_DETECTED (downstream): blocking scale_up, "
                        "using no_op",
                        self.service,
                    )
                    return 0  # no_op

        # Rule 3: UPSTREAM_RECOVERING - block scale_down
        if cmd.command_type == CommandType.UPSTREAM_RECOVERING:
            if rl_action == 2:  # scale_down
                log.info(
                    "[%s] UPSTREAM_RECOVERING: blocking scale_down, using no_op",
                    self.service,
                )
                return 0  # no_op

        # Rule 4: SCALE_UP_RECOMMENDED - consider the recommendation
        if cmd.command_type == CommandType.SCALE_UP_RECOMMENDED:
            # If RL already suggests scale_up, keep it
            if rl_action == 1:
                return rl_action
            # If recommended action is scale_up and service is under load, consider it
            if cmd.recommended_action == 1:
                cfg = get_service_config(self.service)
                if state.cpu_percent > cfg.get("cpu_high", 80):
                    log.info(
                        "[%s] SCALE_UP_RECOMMENDED: using recommended scale_up (cpu=%.1f%%)",
                        self.service,
                        state.cpu_percent,
                    )
                    return 1

        # Default: use RL prediction
        return rl_action

    def report_to_core(self, result: CommandResult) -> None:
        """
        Report execution result back to Core Agent.

        If Core Agent is unavailable (standalone mode), logs the result locally.
        Detects Core Agent unavailability and triggers failover to standalone mode.

        Args:
            result: Command execution result

        Requirements: 5.5, 14.1
        """
        if self.standalone_mode or self.core_agent_ref is None:
            return
        try:
            self._last_result = result
            log.debug( # Đổi INFO thành DEBUG
                "[%s] Reporting to Core Agent: success=%s, action=%s, blocked_reason=%s",
                self.service,
                result.success,
                result.action_executed,
                result.blocked_reason,
            )
            self._core_agent_unavailable_count = 0
        except Exception as e:
            self._core_agent_unavailable_count += 1
            if self._core_agent_unavailable_count >= 3:
                self._switch_to_standalone_mode()

    def run_standalone(self) -> None:
        """
        Run in standalone mode (no Core Agent).

        Behaves identically to LiveAgent.run().
        Used when Core Agent is unavailable.

        Requirements: 10.2, 14.1
        """
        log.info(
            "[%s] Starting standalone mode (no Core Agent)",
            self.service,
        )
        self.standalone_mode = True
        self.core_agent_ref = None
        self.pending_command = None

        # Call the parent run() method
        super().run()

    def _switch_to_standalone_mode(self) -> None:
        """
        Switch to standalone mode when Core Agent becomes unavailable.

        This is called automatically when Core Agent communication fails
        repeatedly. The agent will continue operating independently and
        periodically attempt to reconnect.

        Requirements: 14.1
        """
        log.warning(
            "[%s] Switching to standalone mode due to Core Agent unavailability",
            self.service,
        )
        self.standalone_mode = True
        self.pending_command = None
        self._last_reconnection_attempt = time.time()

    def _attempt_reconnection(self) -> None:
        """
        Periodically attempt to reconnect to Core Agent.

        Checks if enough time has passed since last reconnection attempt,
        and if so, tries to verify Core Agent availability.

        Requirements: 14.1
        """
        if not self.standalone_mode or self.core_agent_ref is None:
            return

        current_time = time.time()
        if current_time - self._last_reconnection_attempt < self._reconnection_attempt_interval:
            return

        self._last_reconnection_attempt = current_time

        try:
            # Attempt to verify Core Agent is available
            # Simple check: see if core_agent_ref is still valid
            if self.core_agent_ref is not None:
                # Try to access a simple attribute to verify it's alive
                _ = self.core_agent_ref.step_count
                
                # If we get here, Core Agent is available again
                log.info(
                    "[%s] Core Agent reconnected, switching back to hierarchical mode",
                    self.service,
                )
                self.standalone_mode = False
                self._core_agent_unavailable_count = 0
        except Exception as e:
            log.debug(
                "[%s] Reconnection attempt failed: %s",
                self.service,
                str(e),
            )
            # Stay in standalone mode on failure

    def _tick(self) -> None:
        """
        Override _tick to integrate command processing.
        """
        self.step_count += 1
        cfg = get_service_config(self.service)

        try:
            # 0. Attempt reconnection if in standalone mode
            if self.standalone_mode:
                self._attempt_reconnection()

            # 1. Observe (Bắt buộc dùng cách này để KHÔNG QUERY PROMETHEUS LẦN 2)
            state = None
            if self.pending_command and "state" in self.pending_command.context:
                state = self.pending_command.context["state"]
                
            if state is None:
                state = self.monitor.get_state(self.service)
                
            obs = self.monitor.to_vector(state)

            # 2. Check auto re-enable RL
            self._check_auto_reenable(state)

            # 3. Get RL prediction
            use_rl_now = (
                self.use_rl
                and self.model is not None
                and not self._rl_disabled_by_rollback
            )

            if use_rl_now:
                if self._model_type == "onnx":
                    rl_action = self.model.predict(obs)
                else:
                    rl_action, _ = self.model.predict(obs, deterministic=True)
                    rl_action = int(rl_action)
                decision_source = "rl"
            else:
                rl_action = self._rule_based_action(state)
                decision_source = "rules"
                if self.use_rl and self._rl_disabled_by_rollback:
                    decision_source = "rules (rollback)"

            # 4. Merge command with RL prediction
            action = rl_action
            if self.pending_command is not None and not self.standalone_mode:
                action = self._merge_command_with_rl(
                    self.pending_command, state, rl_action
                )
                if action != rl_action:
                    decision_source = "rl+command"
                    
            bottleneck_service = None
            if self.pending_command:
                bottleneck_service = self.pending_command.bottleneck_service

            # Lấy thông tin tier từ lệnh của CoreAgent để in Log
            tier_info = "?"
            if self.pending_command and "tier" in self.pending_command.context:
                tier_info = self.pending_command.context["tier"]
            else:
                tier_info = cfg.get("tier", "?")
                
            # Clear command sau khi đã merge và lấy đủ thông tin
            self.pending_command = None

            action_name = ACTIONS.get(action, "unknown")

            # === [1] Non-scalable service: redirect scale → resource limit ===
            # Phải chạy TRƯỚC limit checks để action 1→4, 6→5 không bị chặn
            if not cfg.get("scalable", True):
                if action == 1:
                    action, action_name = 4, "increase_cpu_limit"
                    log.info("[%s] scale_up → increase_cpu_limit (non-scalable)", self.service)
                elif action == 2:
                    action, action_name = 0, "no_op (non-scalable)"
                elif action == 6:
                    action, action_name = 5, "increase_mem_limit"
                    log.info("[%s] kill_and_replace → increase_mem_limit (non-scalable)", self.service)

            # === [2] Tier-1 protection: CẤM action phá hủy cho DB ===
            if str(tier_info) == "1" and action == 6:
                log.warning("[%s] BLOCKED %s: Cấm tuyệt đối cho Tier-1 (DB)", self.service, action_name)
                action, action_name = 0, "no_op (tier1_protected)"

            # === [3] Limit checks ===
            if action == 2 and state.container_count <= cfg["min_containers"]:
                action = 0
                action_name = "no_op (min_limit)"
            elif action == 1 and state.container_count >= cfg["max_containers"]:
                action = 0
                action_name = "no_op (max_limit)"
            elif action == 6 and cfg.get("mem_warning", 80) <= state.memory_percent <= cfg.get("mem_danger", 90):
                action, action_name = 5, "increase_mem_limit (mem_warning)"
            elif action == 6 and state.memory_percent <= cfg.get("mem_danger", 90):
                action, action_name = 0, "no_op (mem_safe)"

            health_ratio = state.healthy_count / max(state.container_count, 1)

            # 6. Update metrics gauges
            self.metrics.update_gauges(
                service=self.service,
                mode="rl" if use_rl_now else ("disabled" if self._rl_disabled_by_rollback else "rules"),
                containers=state.container_count,
                cpu=state.cpu_percent,
                memory=state.memory_percent,
                health_ratio=health_ratio,
            )

            # 7. Cooldown check
            cooldown_info = ""
            was_blocked_by_cooldown = False
            if not action_name.startswith("no_op"):
                can_act, remaining = self.cooldown.can_act(self.service, state=state)
                if not can_act:
                    is_emergency = (
                        state.cpu_percent > 90
                        or state.memory_percent > cfg.get("mem_danger", 90)
                        or health_ratio < 0.7
                    )
                    if is_emergency:
                        log.warning("[%s] Emergency bypass: ignoring cooldown", self.service)
                    else:
                        self.metrics.record_cooldown_blocked(self.service)
                        cooldown_info = f" [cooldown {remaining:.0f}s]"
                        action_name = "no_op (cooldown)"
                        action = 0
                        was_blocked_by_cooldown = True

            if decision_source == "rules (rollback)" and not was_blocked_by_cooldown:
                self.metrics.record_fallback(self.service)

            # 8. Log status (THÊM THÔNG TIN TIER VÀ SERVICE NAME)
            status = "OK"
            if state.cpu_percent > cfg["cpu_high"] or state.memory_percent > cfg["mem_danger"]:
                status = "WARN"
            if state.error_rate > 0.2 or state.response_time_ms > 1500:
                status = "WARN"
                
            mode_tag = "[DRY] " if self.is_dry_run else ""
            standalone_tag = "[STANDALONE] " if self.standalone_mode else ""
            
            log.info(
                "[%04d] [T%s] %s %s %s%sCPU=%.1f%% MEM=%.1f%% DL=%.0fms ERR=%.0f%% Cnt=%d H=%d/%d → %s (%s)%s",
                self.step_count,
                tier_info,
                self.service,
                status,
                mode_tag,
                standalone_tag,
                state.cpu_percent,
                state.memory_percent,
                state.response_time_ms,
                state.error_rate * 100,
                state.container_count,
                state.healthy_count,
                state.container_count,
                action_name,
                decision_source,
                cooldown_info,
            )

            # 9. Act or log (FIX LỖI BÁO action=None KHI CHẠY NO_OP)
            action_executed = action_name if action_name.startswith("no_op") else None
            blocked_reason = None

            if not action_name.startswith("no_op"):
                if self.is_dry_run:
                    self.dryrun_logger.log_suggestion(
                        service=self.service, action=action, action_name=action_name,
                        state_dict={"cpu": round(state.cpu_percent, 2), "mem": round(state.memory_percent, 2), "containers": state.container_count, "healthy": state.healthy_count},
                        decision_source=decision_source
                    )
                    self.metrics.record_dryrun(self.service)
                    action_executed = action_name
                else:
                    self.rollback.snapshot_before_action(self.service, state, action_name)
                    executed = self._execute_action(action, state)
                    if executed:
                        self.cooldown.record_action(self.service, action_name)
                        action_executed = action_name
                    else:
                        blocked_reason = "limits_exceeded"

                self.metrics.record_action(self.service, action_name)

            # 10. Rollback check (live mode only)
            if not self.is_dry_run and not self._rl_disabled_by_rollback:
                should_rollback, reason = self.rollback.check_post_action(self.service, state)
                if should_rollback:
                    self._handle_rollback(reason)
                    blocked_reason = "rollback_triggered"

            # 11. Report to Core Agent
            if not self.standalone_mode:
                result = CommandResult(
                    service=self.service,
                    success=action_executed is not None or action_name.startswith("no_op"),
                    action_executed=action_executed,
                    blocked_reason=blocked_reason,
                )
                self.report_to_core(result)

            # 11.5 Auto-recovery resource cho non-scalable services
            if not self.is_dry_run and not cfg.get("scalable", True):
                if (state.cpu_percent < cfg["cpu_low"]
                    and state.memory_percent < cfg.get("mem_warning", 80)
                    and state.error_rate < 0.05):
                    can_act, _ = self.cooldown.can_act(self.service, state=state)
                    if can_act:
                        base = self.docker_ctl.get_base_limits(self.service)
                        decreased = False
                        if self.docker_ctl.decrease_cpu_limit(self.service, min_cpus=base["cpus"]):
                            decreased = True
                        if self.docker_ctl.decrease_mem_limit(self.service, min_mb=base["memory_mb"]):
                            decreased = True
                        if decreased:
                            self.cooldown.record_action(self.service, "resource_recovery")
                            log.info("[%s] Auto-recovery: giảm resource → base (cpu=%.2f, mem=%dMB)",
                                     self.service, base["cpus"], base["memory_mb"])

            # 12. Append to log
            self.action_log.append({
                "step": self.step_count,
                "time": datetime.now().isoformat(),
                "mode": self.effective_mode,
                "decision_source": decision_source,
                "standalone": self.standalone_mode,
                "bottleneck": bottleneck_service,
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

            # Flush log periodic
            if self.step_count % 1 == 0:
                self._flush_log()

        except Exception as e:
            log.error("[%04d] Error: %s", self.step_count, e, exc_info=True)
            self.metrics.record_error(self.service)
            if not self.standalone_mode:
                result = CommandResult(service=self.service, success=False, blocked_reason=f"error: {str(e)}")
                self.report_to_core(result)
