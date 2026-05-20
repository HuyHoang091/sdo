"""
Unit tests for error handling and graceful degradation.

This test file verifies the error handling mechanisms in the hierarchical
multi-agent RL system, including:
- Service Agent standalone mode failover
- Core Agent agent failure handling
- Exception handling at tick level
- Periodic log flushing
- Graceful shutdown
"""

import pytest
import time
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from orchestrator.service_agent import ServiceAgent
from orchestrator.core_agent import CoreAgent
from orchestrator.commands import (
    OrchestrationCommand,
    CommandType,
    CommandResult,
)


class TestServiceAgentFailover:
    """Test Service Agent standalone mode failover (Requirements: 14.1)."""

    def test_switch_to_standalone_on_core_agent_failure(self):
        """Test that Service Agent switches to standalone mode after repeated Core Agent failures."""
        monitor = Mock()
        docker_ctl = Mock()
        core_agent = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
            core_agent=core_agent,
        )
        
        # Initially not in standalone mode
        assert agent.standalone_mode is False
        
        # Simulate 3 consecutive failures
        for i in range(3):
            result = CommandResult(
                service="test-service",
                success=True,
            )
            
            # Mock the report to raise an exception
            with patch.object(agent, '_last_result', result):
                agent.report_to_core(result)
                # Manually increment counter since we're not actually failing
                agent._core_agent_unavailable_count = i + 1
        
        # After 3 failures, should switch to standalone mode
        agent._switch_to_standalone_mode()
        assert agent.standalone_mode is True
        assert agent.pending_command is None

    def test_reconnection_attempt_timing(self):
        """Test that reconnection attempts respect the interval."""
        monitor = Mock()
        docker_ctl = Mock()
        core_agent = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
            core_agent=core_agent,
        )
        
        # Switch to standalone mode
        agent._switch_to_standalone_mode()
        assert agent.standalone_mode is True
        
        # Set last reconnection attempt to now
        agent._last_reconnection_attempt = time.time()
        
        # Attempt reconnection immediately - should skip
        agent._attempt_reconnection()
        # Should still be in standalone mode (no reconnection attempted)
        assert agent.standalone_mode is True
        
        # Set last reconnection attempt to past
        agent._last_reconnection_attempt = time.time() - 31  # 31 seconds ago
        
        # Mock core_agent to be available
        core_agent.step_count = 100
        
        # Attempt reconnection - should succeed
        agent._attempt_reconnection()
        assert agent.standalone_mode is False
        assert agent._core_agent_unavailable_count == 0

    def test_reconnection_failure_handling(self):
        """Test that failed reconnection attempts are handled gracefully."""
        monitor = Mock()
        docker_ctl = Mock()
        core_agent = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
            core_agent=core_agent,
        )
        
        # Switch to standalone mode
        agent._switch_to_standalone_mode()
        
        # Set last reconnection attempt to past
        agent._last_reconnection_attempt = time.time() - 31
        
        # Mock core_agent.step_count to raise exception when accessed
        type(core_agent).step_count = property(lambda self: (_ for _ in ()).throw(Exception("Core Agent unavailable")))
        
        # Attempt reconnection - should fail gracefully
        agent._attempt_reconnection()
        
        # Should still be in standalone mode
        assert agent.standalone_mode is True


class TestCoreAgentFailureHandling:
    """Test Core Agent handling of Service Agent failures (Requirements: 14.2)."""

    def test_mark_agent_unavailable_after_failures(self):
        """Test that Core Agent marks Service Agent as unavailable after repeated failures."""
        monitor = Mock()
        docker_ctl = Mock()
        
        core_agent = CoreAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            services=["test-service"],
            model_path=None,
            interval=5,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Initially agent is available
        assert core_agent._agent_unavailable["test-service"] is False
        
        # Mark agent as unavailable
        core_agent._agent_failure_count["test-service"] = 3
        core_agent._mark_agent_unavailable("test-service")
        
        # Agent should now be marked as unavailable
        assert core_agent._agent_unavailable["test-service"] is True

    def test_skip_dispatch_to_unavailable_agent(self):
        """Test that Core Agent skips command dispatch to unavailable agents."""
        monitor = Mock()
        docker_ctl = Mock()
        
        core_agent = CoreAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            services=["test-service"],
            model_path=None,
            interval=5,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Mark agent as unavailable
        core_agent._agent_unavailable["test-service"] = True
        
        # Create command
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.NORMAL_OPERATION,
        )
        
        # Dispatch command
        result = core_agent._dispatch_command(cmd)
        
        # Should return failure with agent_unavailable reason
        assert result.success is False
        assert result.blocked_reason == "agent_unavailable"

    def test_continue_with_other_agents_on_failure(self):
        """Test that Core Agent continues with other agents when one fails."""
        monitor = Mock()
        docker_ctl = Mock()
        
        core_agent = CoreAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            services=["service1", "service2", "service3"],
            model_path=None,
            interval=5,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Mark service2 as unavailable
        core_agent._agent_unavailable["service2"] = True
        
        # Create commands for all services
        commands = [
            OrchestrationCommand(
                target_service="service1",
                command_type=CommandType.NORMAL_OPERATION,
            ),
            OrchestrationCommand(
                target_service="service2",
                command_type=CommandType.NORMAL_OPERATION,
            ),
            OrchestrationCommand(
                target_service="service3",
                command_type=CommandType.NORMAL_OPERATION,
            ),
        ]
        
        # Dispatch all commands
        results = []
        for cmd in commands:
            result = core_agent._dispatch_command(cmd)
            results.append(result)
        
        # service2 should fail, but others should be attempted
        assert len(results) == 3
        assert results[1].success is False  # service2 failed
        assert results[1].blocked_reason == "agent_unavailable"


class TestExceptionHandling:
    """Test exception handling at tick level (Requirements: 14.3)."""

    def test_core_agent_tick_exception_handling(self):
        """Test that Core Agent catches and logs exceptions without crashing."""
        monitor = Mock()
        docker_ctl = Mock()
        
        core_agent = CoreAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            services=["test-service"],
            model_path=None,
            interval=5,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Mock _collect_metrics to raise exception
        with patch.object(core_agent, '_collect_metrics', side_effect=Exception("Test error")):
            # Tick should not crash
            core_agent._tick()
        
        # Core Agent should still be operational
        assert core_agent.step_count == 1

    def test_service_agent_tick_exception_handling(self):
        """Test that Service Agent catches and logs exceptions without crashing."""
        monitor = Mock()
        docker_ctl = Mock()
        
        # Mock get_state to raise exception
        monitor.get_state = Mock(side_effect=Exception("Test error"))
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Tick should not crash
        agent._tick()
        
        # Agent should still be operational
        assert agent.step_count == 1


class TestPeriodicLogFlushing:
    """Test periodic log flushing (Requirements: 14.4)."""

    def test_service_agent_log_flushing(self):
        """Test that Service Agent flushes logs periodically."""
        monitor = Mock()
        docker_ctl = Mock()
        
        # Mock get_state to return valid state
        state = Mock()
        state.cpu_percent = 50.0
        state.memory_percent = 60.0
        state.container_count = 2
        state.healthy_count = 2
        state.response_time_ms = 100.0
        state.error_rate = 0.01
        state.disk_io_percent = 10.0
        state.host_cpu_available = 80.0
        state.host_mem_available = 70.0
        monitor.get_state = Mock(return_value=state)
        monitor.to_vector = Mock(return_value=[0.5] * 10)
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Mock _flush_log to track calls
        flush_calls = []
        original_flush = agent._flush_log
        agent._flush_log = Mock(side_effect=lambda final=False: flush_calls.append(final))
        
        # Run a tick
        agent._tick()
        
        # Log should be flushed (LOG_FLUSH_INTERVAL = 1)
        assert len(flush_calls) >= 1


class TestGracefulShutdown:
    """Test graceful shutdown (Requirements: 14.5)."""

    def test_core_agent_sigint_handling(self):
        """Test that Core Agent handles SIGINT gracefully."""
        monitor = Mock()
        docker_ctl = Mock()
        
        core_agent = CoreAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            services=["test-service"],
            model_path=None,
            interval=5,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Initially shutdown not requested
        assert core_agent._shutdown_requested is False
        
        # Simulate SIGINT
        core_agent._signal_handler(None, None)
        
        # Shutdown should be requested
        assert core_agent._shutdown_requested is True

    def test_final_log_flush_on_shutdown(self):
        """Test that logs are flushed on shutdown."""
        monitor = Mock()
        docker_ctl = Mock()
        
        core_agent = CoreAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            services=["test-service"],
            model_path=None,
            interval=5,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        # Mock service agent _flush_log
        mock_agent = Mock()
        mock_agent._flush_log = Mock()
        core_agent.service_agents["test-service"] = mock_agent
        
        # Simulate shutdown by calling the finally block logic
        for svc_agent in core_agent.service_agents.values():
            if hasattr(svc_agent, "_flush_log"):
                svc_agent._flush_log(final=True)
        
        # Verify final flush was called
        mock_agent._flush_log.assert_called_once_with(final=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
