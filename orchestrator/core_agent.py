"""
Core Agent - Central coordinator for hierarchical multi-agent RL orchestration.

This module implements the CoreAgent class that serves as the central coordinator
in the hierarchical multi-agent architecture. The Core Agent is responsible for:
1. Collecting metrics from all services
2. Detecting bottleneck services using ServiceTopology
3. Generating orchestration commands based on fleet-wide analysis
4. Dispatching commands to Service Agents
5. Handling circuit breaker for host overload
6. Implementing main run loop and tick orchestration

Requirements: 1.1-3.6, 4.1-4.5, 11.1
"""

import logging
import os
import psutil
import signal
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from orchestrator.agent_metrics import get_metrics, start_metrics_server
from orchestrator.commands import CommandResult, CommandType, OrchestrationCommand
from orchestrator.config import (
    ACTIONS,
    AGENT_METRICS_PORT,
    CIRCUIT_BREAKER_FRONTEND,
    CIRCUIT_BREAKER_RECOVERY_THRESHOLD,
    HOST_CPU_CRITICAL,
    HOST_MEM_CRITICAL,
    MULTI_AGENT_INTERVAL,
    SERVICE_CONFIGS,
    get_service_config,
)
from orchestrator.guardrails import CooldownManager, RollbackGuard
from orchestrator.monitor import ServiceState
from orchestrator.reward import UnifiedRewardFunction
from orchestrator.service_agent import ServiceAgent
from orchestrator.topology import ServiceTopology

log = logging.getLogger(__name__)


class CoreAgent:
    """
    Central coordinator for hierarchical multi-agent orchestration.

    Responsibilities:
    1. Collect metrics from all services
    2. Detect bottleneck services using ServiceTopology
    3. Generate orchestration commands based on fleet-wide analysis
    4. Dispatch commands to Service Agents
    5. Track command delivery and execution status

    Requirements: 1.1, 11.1
    """

    def __init__(
        self,
        monitor,
        docker_ctl,
        services: List[str],
        model_path: Optional[str] = None,
        interval: int = None,
        use_rl: bool = True,
        mode: str = None,
        enable_metrics: bool = True,
    ) -> None:
        """
        Initialize Core Agent.

        Args:
            monitor: Metrics collection interface
            docker_ctl: Docker control interface
            services: List of service names to manage
            model_path: Path to RL model (optional)
            interval: Tick interval in seconds
            use_rl: Whether to use RL model
            mode: Agent mode ("live", "dry-run", "shadow")
            enable_metrics: Whether to enable Prometheus metrics

        Requirements: 1.1, 11.1
        """
        self.monitor = monitor
        self.docker_ctl = docker_ctl
        self.services = services
        self.interval = interval or MULTI_AGENT_INTERVAL
        self.use_rl = use_rl
        self.mode = mode or os.getenv("SDO_MODE", "live")
        self.step_count = 0

        # Initialize topology with managed services only
        managed_configs = {
            s: SERVICE_CONFIGS[s] for s in services if s in SERVICE_CONFIGS
        }
        self.topology = ServiceTopology(managed_configs)

        # Initialize unified reward function
        self.reward_fn = UnifiedRewardFunction(self.topology)

        # Initialize Service Agents for each service
        self.service_agents: Dict[str, ServiceAgent] = {}
        for svc in services:
            self.service_agents[svc] = ServiceAgent(
                monitor=monitor,
                docker_ctl=docker_ctl,
                service=svc,
                model_path=model_path,
                interval=interval,
                use_rl=use_rl,
                mode=mode,
                enable_metrics=False,  # Core Agent manages metrics
                core_agent=self,
            )

        # Per-service guardrails (managed by Core Agent)
        self.cooldowns: Dict[str, CooldownManager] = {
            svc: CooldownManager() for svc in services
        }
        self.rollbacks: Dict[str, RollbackGuard] = {
            svc: RollbackGuard() for svc in services
        }

        # Track Service Agent availability (Requirements: 14.2)
        self._agent_unavailable: Dict[str, bool] = {svc: False for svc in services}
        self._agent_failure_count: Dict[str, int] = {svc: 0 for svc in services}

        # Metrics (shared singleton)
        self.metrics = get_metrics()
        if enable_metrics:
            start_metrics_server(self.metrics, AGENT_METRICS_PORT)

        # Circuit breaker state
        self._circuit_breaker_active: bool = False

        # Graceful shutdown handler
        signal.signal(signal.SIGINT, self._signal_handler)
        self._shutdown_requested = False

        log.info("CoreAgent initialized with %d services", len(services))

    @property
    def is_dry_run(self) -> bool:
        """Check if running in dry-run mode."""
        return self.mode in ("dry-run", "shadow")

    def _signal_handler(self, signum, frame):
        """Handle SIGINT for graceful shutdown."""
        log.info("Received SIGINT, initiating graceful shutdown...")
        self._shutdown_requested = True

    def run(self) -> None:
        """
        Main loop - runs until interrupted.

        Steps:
        1. Print startup information
        2. Run tick loop at configured interval
        3. Handle graceful shutdown on SIGINT

        Requirements: 1.1
        """
        log.info("=" * 60)
        log.info("SDO Core Agent Started")
        log.info("  Services:  %s", self.services)
        log.info("  Order:     %s", self.topology.get_processing_order())
        log.info("  Interval:  %ds", self.interval)
        log.info("  Mode:      %s", "RL" if self.use_rl else "rule-based")
        log.info("  Dry-run:   %s", "YES" if self.is_dry_run else "NO")
        log.info("  Metrics:   http://localhost:%d/metrics", AGENT_METRICS_PORT)
        self.topology.print_topology()
        log.info("=" * 60)

        try:
            while not self._shutdown_requested:
                self._tick()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log.info("Core Agent interrupted")
        finally:
            log.info("Core Agent stopped after %d steps", self.step_count)
            # Final cleanup
            for svc_agent in self.service_agents.values():
                if hasattr(svc_agent, "_flush_log"):
                    svc_agent._flush_log(final=True)

    def _tick(self) -> None:
        """
        Single tick of the orchestration loop.

        Steps:
        1. Check circuit breaker (host overload)
        2. Collect metrics from all services
        3. Detect bottleneck using ServiceTopology
        4. Generate commands for each service
        5. Dispatch commands to Service Agents
        6. Handle rollback checks

        Requirements: 1.1-1.5, 2.1-2.5, 3.1-3.6
        """
        self.step_count += 1

        try:
            # Step 1: Check circuit breaker (host overload)
            if self._handle_circuit_breaker():
                # Circuit breaker active, skip this tick
                return

            # Step 2: Collect metrics from all services
            states = self._collect_metrics()

            # Step 3: Detect bottleneck using ServiceTopology
            bottleneck = self._detect_bottleneck(states)

            if bottleneck:
                log.warning(
                    "[%04d] 🚨 BOTTLENECK: %s (tier %d)",
                    self.step_count,
                    bottleneck,
                    self.topology.get_tier(bottleneck),
                )

            # Step 4: Generate commands for each service
            commands = self._generate_commands(bottleneck, states)

            # Step 5: Dispatch commands to Service Agents
            results = []
            for cmd in commands:
                result = self._dispatch_command(cmd)
                results.append(result)

            # Step 6: Log results
            for result in results:
                if not result.success:
                    log.warning(
                        "[%s] Command failed: %s",
                        result.service,
                        result.blocked_reason,
                    )

        except Exception as e:
            log.error("[%04d] CoreAgent tick error: %s", self.step_count, e, exc_info=True)
            self.metrics.record_error("core_agent")

    def _collect_metrics(self) -> Dict[str, ServiceState]:
        """
        Query metrics from all configured services.

        Handles partial failures gracefully - continues with available data.
        Logs warnings for services that fail to report metrics.

        Returns:
            Dictionary mapping service name to ServiceState

        Requirements: 1.1, 1.2, 1.3
        """
        states = {}

        for svc in self.services:
            try:
                state = self.monitor.get_state(svc)
                states[svc] = state

                # Update metrics gauges
                health_ratio = state.healthy_count / max(state.container_count, 1)
                self.metrics.update_gauges(
                    service=svc,
                    mode="rl" if self.use_rl else "rules",
                    containers=state.container_count,
                    cpu=state.cpu_percent,
                    memory=state.memory_percent,
                    health_ratio=health_ratio,
                )

            except Exception as e:
                log.warning(
                    "[%s] Failed to collect metrics: %s",
                    svc,
                    str(e),
                )
                # Continue with other services (partial failure handling)
                continue

        return states

    def _handle_circuit_breaker(self) -> bool:
        """
        Check host CPU and memory against critical thresholds.

        When host resources exceed critical thresholds (90%), triggers circuit
        breaker protection by removing all services except frontend from load balancer.

        Returns:
            True if circuit breaker is active (skip tick), False otherwise

        Requirements: 1.5
        """
        host_cpu = psutil.cpu_percent(interval=0.5)
        host_mem = psutil.virtual_memory().percent

        # Check if we need to activate circuit breaker
        if host_cpu > HOST_CPU_CRITICAL or host_mem > HOST_MEM_CRITICAL:
            if not self._circuit_breaker_active:
                self._circuit_breaker_active = True
                log.critical(
                    "🔴 CIRCUIT BREAKER ON: Host CPU=%.0f%% MEM=%.0f%% "
                    "— Removing all services except %s from LB",
                    host_cpu,
                    host_mem,
                    CIRCUIT_BREAKER_FRONTEND,
                )

                # Remove all services except frontend from load balancer
                for svc in self.services:
                    if svc == CIRCUIT_BREAKER_FRONTEND:
                        continue
                    try:
                        for cid in self.docker_ctl.get_container_ids(svc):
                            self.docker_ctl.update_load_balancer(
                                svc, exclude_container_id=cid
                            )
                        log.warning("  [%s] Removed from LB (circuit breaker)", svc)
                    except Exception as e:
                        log.error("  [%s] Failed to remove from LB: %s", svc, e)

            log.warning(
                "[%04d] 🔴 CIRCUIT BREAKER ACTIVE — Host CPU=%.0f%% MEM=%.0f%% — Waiting for recovery...",
                self.step_count,
                host_cpu,
                host_mem,
            )
            return True  # Skip tick

        # Check if we can deactivate circuit breaker
        if self._circuit_breaker_active:
            if (
                host_cpu < CIRCUIT_BREAKER_RECOVERY_THRESHOLD
                and host_mem < CIRCUIT_BREAKER_RECOVERY_THRESHOLD
            ):
                self._circuit_breaker_active = False
                log.critical(
                    "🟢 CIRCUIT BREAKER OFF: Host CPU=%.0f%% MEM=%.0f%% "
                    "— Restoring services to LB",
                    host_cpu,
                    host_mem,
                )

                # Restore all services to load balancer
                for svc in self.services:
                    try:
                        self.docker_ctl.update_load_balancer(svc)
                        log.info("  [%s] Restored to LB", svc)
                    except Exception as e:
                        log.error("  [%s] Failed to restore to LB: %s", svc, e)
            else:
                log.warning(
                    "[%04d] 🟡 CIRCUIT BREAKER — Recovering Host CPU=%.0f%% MEM=%.0f%%",
                    self.step_count,
                    host_cpu,
                    host_mem,
                )
                return True  # Still active, skip tick

        return False  # Circuit breaker not active

    def _detect_bottleneck(self, states: Dict[str, ServiceState]) -> Optional[str]:
        """
        Detect bottleneck service using ServiceTopology.

        Records current tick state in topology and retrieves cached bottleneck result.

        Args:
            states: Current fleet state

        Returns:
            Name of bottleneck service, or None if no bottleneck

        Requirements: 2.1
        """
        # Record tick in topology for bottleneck detection
        self.topology.record_tick(states)

        # Get cached bottleneck result
        bottleneck = self.topology.detect_bottleneck()

        return bottleneck

    def _generate_commands(
        self,
        bottleneck: Optional[str],
        states: Dict[str, ServiceState],
    ) -> List[OrchestrationCommand]:
        """
        Generate orchestration commands for all services.

        Processes services in topological order (Tier-1 first) and generates
        appropriate commands based on bottleneck status and service state.

        Args:
            bottleneck: Current bottleneck service (if any)
            states: Current fleet state

        Returns:
            List of commands in processing order (topological sort)

        Requirements: 3.1, 3.6
        """
        commands = []
        order = self.topology.get_processing_order()

        # Calculate fleet-wide context
        fleet_avg_cpu = (
            sum(s.cpu_percent for s in states.values()) / len(states)
            if states
            else 0.0
        )
        fleet_avg_health = (
            sum(
                s.healthy_count / max(s.container_count, 1) for s in states.values()
            )
            / len(states)
            if states
            else 1.0
        )

        for svc in order:
            state = states.get(svc)
            if state is None:
                continue

            # Determine command type based on bottleneck status
            if svc == bottleneck:
                cmd_type = CommandType.EMERGENCY_PRIORITY
            elif bottleneck and self.topology.is_downstream_of_bottleneck(svc):
                cmd_type = CommandType.BOTTLENECK_DETECTED
            elif self.topology.is_upstream_recovering(svc, states):
                cmd_type = CommandType.UPSTREAM_RECOVERING
            else:
                cmd_type = CommandType.NORMAL_OPERATION

            # Calculate recommended action
            recommended = self._calculate_recommended_action(svc, state, bottleneck)

            # Apply bottleneck filter
            filtered_action, action_name = self._apply_bottleneck_filter(
                svc, recommended, bottleneck, states
            )

            # Create command
            cmd = OrchestrationCommand(
                target_service=svc,
                command_type=cmd_type,
                bottleneck_service=bottleneck,
                cascade_impact=(
                    self.topology.get_cascade_impact(bottleneck) if bottleneck else []
                ),
                recommended_action=filtered_action,
                context={
                    "fleet_avg_cpu": fleet_avg_cpu,
                    "fleet_avg_health": fleet_avg_health,
                    "tier": self.topology.get_tier(svc),
                    "state": state,
                },
            )

            commands.append(cmd)

        return commands

    def _calculate_recommended_action(
        self,
        service: str,
        state: ServiceState,
        bottleneck: Optional[str],
    ) -> int:
        """
        Calculate recommended action for a service.

        Uses rule-based logic to determine appropriate action based on service state.

        Args:
            service: Service name
            state: Current service state
            bottleneck: Current bottleneck service (if any)

        Returns:
            Recommended action (0-6)
        """
        cfg = get_service_config(service)

        # Priority 1: Health issues
        if state.healthy_count < state.container_count:
            return 3  # restart_unhealthy

        # Priority 2: Critical errors
        if state.error_rate > 0.5:
            return 6  # kill_and_replace
        elif state.error_rate > 0.3:
            return 3  # restart_unhealthy

        # Priority 3: Resource issues
        if state.memory_percent > cfg.get("mem_danger", 90):
            return 6  # kill_and_replace

        # Priority 4: Performance issues
        if state.cpu_percent > cfg.get("cpu_high", 80):
            return 1  # scale_up
        if state.response_time_ms > 1500:
            return 1  # scale_up

        # Priority 5: Scale down opportunity
        if (
            state.cpu_percent < cfg.get("cpu_low", 30)
            and state.container_count > cfg["min_containers"]
            and state.response_time_ms < 500
            and state.error_rate < 0.05
        ):
            return 2  # scale_down

        return 0  # no_op

    def _apply_bottleneck_filter(
        self,
        service: str,
        action: int,
        bottleneck: Optional[str],
        states: Dict[str, ServiceState],
    ) -> tuple[int, str]:
        """
        Apply bottleneck-aware action filtering.

        Rules:
        1. Block scale_up for downstream services when bottleneck exists
        2. Block scale_down when upstream is recovering

        Args:
            service: Target service
            action: Proposed action
            bottleneck: Current bottleneck service
            states: Fleet state

        Returns:
            Tuple of (filtered_action, action_name)

        Requirements: 3.3, 3.4
        """
        action_name = ACTIONS.get(action, "unknown")

        # Rule 1: Block scale_up for downstream services when bottleneck exists
        if bottleneck and self.topology.is_downstream_of_bottleneck(service):
            if action == 1:  # scale_up
                log.info(
                    "  [%s] BLOCKED scale_up: downstream of bottleneck %s",
                    service,
                    bottleneck,
                )
                return 0, "no_op (downstream_blocked)"

        # Rule 2: Block scale_down when upstream is recovering
        if action == 2:  # scale_down
            if self.topology.is_upstream_recovering(service, states):
                log.info(
                    "  [%s] BLOCKED scale_down: upstream recovering",
                    service,
                )
                return 0, "no_op (upstream_recovering)"

        return action, action_name

    def _dispatch_command(self, cmd: OrchestrationCommand) -> CommandResult:
        """
        Dispatch command to target Service Agent.

        Sends the command to the appropriate Service Agent and retrieves the result.
        Handles agent unavailability gracefully by logging failures and continuing
        with other agents.

        Args:
            cmd: Command to dispatch

        Returns:
            Result of command execution

        Requirements: 4.1, 4.4, 14.2
        """
        service = cmd.target_service

        # Check if Service Agent is marked as unavailable
        if self._agent_unavailable.get(service, False):
            log.debug(
                "[%s] Service Agent marked as unavailable, skipping command dispatch",
                service,
            )
            return CommandResult(
                service=service,
                success=False,
                blocked_reason="agent_unavailable",
            )

        # Check if Service Agent exists
        if service not in self.service_agents:
            log.error(
                "[%s] Service Agent not found for command dispatch",
                service,
            )
            self._mark_agent_unavailable(service)
            return CommandResult(
                service=service,
                success=False,
                blocked_reason="agent_not_found",
            )

        try:
            # Send command to Service Agent
            agent = self.service_agents[service]
            agent.receive_command(cmd)

            # Trigger agent tick to process command immediately
            agent._tick()

            # Retrieve result from agent
            if hasattr(agent, "_last_result") and agent._last_result:
                result = agent._last_result
                agent._last_result = None  # Clear after retrieval
                
                # Reset failure count on successful dispatch
                self._agent_failure_count[service] = 0
                
                return result
            else:
                # No result available, assume success
                return CommandResult(
                    service=service,
                    success=True,
                    action_executed="no_op",
                )

        except Exception as e:
            # Service Agent failure - log and continue with other agents
            self._agent_failure_count[service] = self._agent_failure_count.get(service, 0) + 1
            
            log.error(
                "[%s] Failed to dispatch command (attempt %d): %s",
                service,
                self._agent_failure_count[service],
                str(e),
                exc_info=True,
            )
            
            # Mark agent as unavailable after 3 consecutive failures
            if self._agent_failure_count[service] >= 3:
                self._mark_agent_unavailable(service)
            
            return CommandResult(
                service=service,
                success=False,
                blocked_reason=f"dispatch_error: {str(e)}",
            )

    def _mark_agent_unavailable(self, service: str) -> None:
        """
        Mark a Service Agent as unavailable.

        This prevents further command dispatch attempts until the agent
        recovers or is manually re-enabled.

        Args:
            service: Service name

        Requirements: 14.2
        """
        if not self._agent_unavailable.get(service, False):
            log.error(
                "[%s] Marking Service Agent as UNAVAILABLE after %d failures",
                service,
                self._agent_failure_count.get(service, 0),
            )
            self._agent_unavailable[service] = True
            self.metrics.record_error(f"agent_{service}")

    def get_fleet_state(self) -> Dict:
        """
        Get current fleet-wide state.

        Returns:
            Dictionary containing fleet state information
        """
        states = self._collect_metrics()
        return {
            "step": self.step_count,
            "services": {
                svc: {
                    "cpu": state.cpu_percent,
                    "memory": state.memory_percent,
                    "containers": state.container_count,
                    "healthy": state.healthy_count,
                    "error_rate": state.error_rate,
                    "response_time_ms": state.response_time_ms,
                }
                for svc, state in states.items()
            },
            "bottleneck": self.topology.detect_bottleneck(),
            "circuit_breaker_active": self._circuit_breaker_active,
        }

    def get_processing_order(self) -> List[str]:
        """
        Get service processing order (topological sort).

        Returns:
            List of service names in processing order
        """
        return self.topology.get_processing_order()
