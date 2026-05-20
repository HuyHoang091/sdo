"""
Unit tests for ServiceAgent implementation.

This test file verifies the ServiceAgent class correctly extends LiveAgent
and implements hierarchical orchestration features.
"""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime

from orchestrator.service_agent import ServiceAgent
from orchestrator.commands import (
    OrchestrationCommand,
    CommandType,
    CommandResult,
)


class TestServiceAgentBasics:
    """Test basic ServiceAgent initialization and configuration."""

    def test_service_agent_initialization(self):
        """Test that ServiceAgent can be initialized with required parameters."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            interval=10,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
            core_agent=None,
        )
        
        assert agent.service == "test-service"
        assert agent.standalone_mode is True
        assert agent.core_agent_ref is None
        assert agent.pending_command is None

    def test_service_agent_with_core_agent(self):
        """Test that ServiceAgent correctly tracks core agent reference."""
        monitor = Mock()
        docker_ctl = Mock()
        core_agent = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            interval=10,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
            core_agent=core_agent,
        )
        
        assert agent.standalone_mode is False
        assert agent.core_agent_ref is core_agent


class TestCommandValidation:
    """Test command validation logic."""

    def test_validate_valid_command(self):
        """Test that valid commands pass validation."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.SCALE_UP_RECOMMENDED,
            recommended_action=1,
        )
        
        assert agent._validate_command(cmd) is True

    def test_validate_wrong_target_service(self):
        """Test that commands for wrong service are rejected."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="wrong-service",
            command_type=CommandType.SCALE_UP_RECOMMENDED,
        )
        
        assert agent._validate_command(cmd) is False

    def test_validate_invalid_action(self):
        """Test that commands with invalid action values are rejected."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.SCALE_UP_RECOMMENDED,
            recommended_action=99,  # Invalid action
        )
        
        assert agent._validate_command(cmd) is False


class TestCommandReception:
    """Test command reception and queuing."""

    def test_receive_valid_command(self):
        """Test that valid commands are queued for processing."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.BOTTLENECK_DETECTED,
            bottleneck_service="database",
        )
        
        agent.receive_command(cmd)
        
        assert agent.pending_command is not None
        assert agent.pending_command.command_type == CommandType.BOTTLENECK_DETECTED

    def test_receive_invalid_command(self):
        """Test that invalid commands are rejected and not queued."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="wrong-service",
            command_type=CommandType.SCALE_UP_RECOMMENDED,
        )
        
        agent.receive_command(cmd)
        
        assert agent.pending_command is None


class TestCommandMerge:
    """Test command merging with RL predictions."""

    def test_merge_emergency_priority(self):
        """Test that EMERGENCY_PRIORITY commands use recommended action."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.EMERGENCY_PRIORITY,
            recommended_action=1,  # scale_up
        )
        
        state = Mock()
        rl_action = 0  # no_op
        
        merged_action = agent._merge_command_with_rl(cmd, state, rl_action)
        
        assert merged_action == 1  # Should use recommended action

    def test_merge_bottleneck_detected_self(self):
        """Test that bottleneck service prefers scale_up actions."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.BOTTLENECK_DETECTED,
            bottleneck_service="test-service",  # This service IS the bottleneck
            recommended_action=1,
        )
        
        state = Mock()
        rl_action = 1  # scale_up
        
        merged_action = agent._merge_command_with_rl(cmd, state, rl_action)
        
        assert merged_action == 1  # Should keep scale_up

    def test_merge_bottleneck_detected_downstream(self):
        """Test that downstream services block scale_up when bottleneck exists."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.BOTTLENECK_DETECTED,
            bottleneck_service="database",  # Different service is bottleneck
        )
        
        state = Mock()
        rl_action = 1  # scale_up
        
        merged_action = agent._merge_command_with_rl(cmd, state, rl_action)
        
        assert merged_action == 0  # Should block scale_up with no_op

    def test_merge_upstream_recovering(self):
        """Test that UPSTREAM_RECOVERING blocks scale_down."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        cmd = OrchestrationCommand(
            target_service="test-service",
            command_type=CommandType.UPSTREAM_RECOVERING,
        )
        
        state = Mock()
        rl_action = 2  # scale_down
        
        merged_action = agent._merge_command_with_rl(cmd, state, rl_action)
        
        assert merged_action == 0  # Should block scale_down with no_op

    def test_merge_no_command(self):
        """Test that RL action is used when no command is present."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
        )
        
        state = Mock()
        rl_action = 3  # restart_unhealthy
        
        merged_action = agent._merge_command_with_rl(None, state, rl_action)
        
        assert merged_action == 3  # Should use RL action


class TestReportToCore:
    """Test reporting results back to Core Agent."""

    def test_report_in_standalone_mode(self):
        """Test that reporting is skipped in standalone mode."""
        monitor = Mock()
        docker_ctl = Mock()
        
        agent = ServiceAgent(
            monitor=monitor,
            docker_ctl=docker_ctl,
            service="test-service",
            model_path=None,
            use_rl=False,
            mode="dry-run",
            enable_metrics=False,
            core_agent=None,
        )
        
        result = CommandResult(
            service="test-service",
            success=True,
            action_executed="scale_up",
        )
        
        # Should not raise any errors
        agent.report_to_core(result)
        
        # In standalone mode, result is just logged, not sent

    def test_report_with_core_agent(self):
        """Test that results are stored when core agent is present."""
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
        
        result = CommandResult(
            service="test-service",
            success=True,
            action_executed="scale_up",
        )
        
        agent.report_to_core(result)
        
        assert agent._last_result is not None
        assert agent._last_result.action_executed == "scale_up"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
