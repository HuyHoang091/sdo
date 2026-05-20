"""
Unit tests for CoreAgent class.

Tests cover:
- Initialization
- Metrics collection
- Circuit breaker logic
- Bottleneck detection
- Command generation
- Command dispatch
- Bottleneck filtering
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from orchestrator.core_agent import CoreAgent
from orchestrator.commands import CommandType, OrchestrationCommand
from orchestrator.monitor import ServiceState


@pytest.fixture
def mock_monitor():
    """Create a mock monitor."""
    monitor = Mock()
    return monitor


@pytest.fixture
def mock_docker_ctl():
    """Create a mock docker controller."""
    docker_ctl = Mock()
    docker_ctl.get_container_ids = Mock(return_value=[])
    docker_ctl.update_load_balancer = Mock()
    return docker_ctl


@pytest.fixture
def sample_services():
    """Sample service list."""
    return ["db", "api", "web-app"]


@pytest.fixture
def mock_service_configs():
    """Mock SERVICE_CONFIGS with test services."""
    return {
        "db": {
            "tier": 1,
            "depends_on": [],
            "min_containers": 1,
            "max_containers": 1,
            "scalable": False,
            "cpu_high": 80,
            "cpu_low": 20,
            "mem_danger": 90,
            "mem_warning": 80,
            "cooldown_seconds": 60,
            "rollback_window_seconds": 300,
            "rollback_error_threshold": 0.1,
            "rollback_latency_factor": 2.0,
            "rollback_action": "rule-based",
        },
        "api": {
            "tier": 2,
            "depends_on": ["db"],
            "min_containers": 1,
            "max_containers": 6,
            "scalable": True,
            "cpu_high": 80,
            "cpu_low": 20,
            "mem_danger": 90,
            "mem_warning": 80,
            "cooldown_seconds": 45,
            "rollback_window_seconds": 300,
            "rollback_error_threshold": 0.1,
            "rollback_latency_factor": 2.0,
            "rollback_action": "rule-based",
        },
        "web-app": {
            "tier": 3,
            "depends_on": ["api"],
            "min_containers": 1,
            "max_containers": 8,
            "scalable": True,
            "cpu_high": 80,
            "cpu_low": 20,
            "mem_danger": 90,
            "mem_warning": 80,
            "cooldown_seconds": 30,
            "rollback_window_seconds": 300,
            "rollback_error_threshold": 0.1,
            "rollback_latency_factor": 2.0,
            "rollback_action": "rule-based",
        },
    }


@pytest.fixture
def sample_state():
    """Create a sample ServiceState."""
    return ServiceState(
        cpu_percent=50.0,
        memory_percent=60.0,
        memory_bytes=1024 * 1024 * 1024,
        network_rx_rate=1000.0,
        container_count=2,
        restart_count=0,
        healthy_count=2,
        host_cpu_available=50.0,
        host_mem_available=40.0,
        response_time_ms=100.0,
        error_rate=0.01,
        disk_io_percent=20.0,
    )


class TestCoreAgentInitialization:
    """Test CoreAgent initialization."""

    def test_init_creates_service_agents(self, mock_monitor, mock_docker_ctl, sample_services, mock_service_configs):
        """Test that CoreAgent creates Service Agents for each service."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        assert len(agent.service_agents) == len(sample_services)
        for svc in sample_services:
            assert svc in agent.service_agents

    def test_init_creates_topology(self, mock_monitor, mock_docker_ctl, sample_services, mock_service_configs):
        """Test that CoreAgent creates ServiceTopology."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        assert agent.topology is not None
        assert len(agent.topology.services) == len(sample_services)

    def test_init_creates_guardrails(self, mock_monitor, mock_docker_ctl, sample_services, mock_service_configs):
        """Test that CoreAgent creates guardrails for each service."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        assert len(agent.cooldowns) == len(sample_services)
        assert len(agent.rollbacks) == len(sample_services)


class TestMetricsCollection:
    """Test metrics collection."""

    def test_collect_metrics_success(self, mock_monitor, mock_docker_ctl, sample_services, sample_state, mock_service_configs):
        """Test successful metrics collection from all services."""
        mock_monitor.get_state = Mock(return_value=sample_state)

        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        states = agent._collect_metrics()

        assert len(states) == len(sample_services)
        for svc in sample_services:
            assert svc in states
            assert states[svc] == sample_state

    def test_collect_metrics_partial_failure(self, mock_monitor, mock_docker_ctl, sample_services, sample_state, mock_service_configs):
        """Test metrics collection with partial failures."""
        def get_state_side_effect(service):
            if service == "api":
                raise Exception("Connection timeout")
            return sample_state

        mock_monitor.get_state = Mock(side_effect=get_state_side_effect)

        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        states = agent._collect_metrics()

        # Should have 2 services (db and web-app), api failed
        assert len(states) == 2
        assert "db" in states
        assert "web-app" in states
        assert "api" not in states


class TestCircuitBreaker:
    """Test circuit breaker logic."""

    @patch("orchestrator.core_agent.psutil")
    def test_circuit_breaker_activates_on_high_cpu(self, mock_psutil, mock_monitor, mock_docker_ctl, sample_services, mock_service_configs):
        """Test circuit breaker activates when host CPU exceeds threshold."""
        mock_psutil.cpu_percent.return_value = 95.0  # Above HOST_CPU_CRITICAL (90%)
        mock_psutil.virtual_memory.return_value = Mock(percent=50.0)

        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        result = agent._handle_circuit_breaker()

        assert result is True  # Circuit breaker active, skip tick
        assert agent._circuit_breaker_active is True

    @patch("orchestrator.core_agent.psutil")
    def test_circuit_breaker_activates_on_high_memory(self, mock_psutil, mock_monitor, mock_docker_ctl, sample_services, mock_service_configs):
        """Test circuit breaker activates when host memory exceeds threshold."""
        mock_psutil.cpu_percent.return_value = 50.0
        mock_psutil.virtual_memory.return_value = Mock(percent=95.0)  # Above HOST_MEM_CRITICAL (90%)

        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        result = agent._handle_circuit_breaker()

        assert result is True
        assert agent._circuit_breaker_active is True

    @patch("orchestrator.core_agent.psutil")
    def test_circuit_breaker_deactivates_on_recovery(self, mock_psutil, mock_monitor, mock_docker_ctl, sample_services, mock_service_configs):
        """Test circuit breaker deactivates when host recovers."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        # Activate circuit breaker
        agent._circuit_breaker_active = True

        # Host recovers
        mock_psutil.cpu_percent.return_value = 50.0
        mock_psutil.virtual_memory.return_value = Mock(percent=50.0)

        result = agent._handle_circuit_breaker()

        assert result is False  # Circuit breaker deactivated
        assert agent._circuit_breaker_active is False


class TestBottleneckDetection:
    """Test bottleneck detection."""

    def test_detect_bottleneck_calls_topology(self, mock_monitor, mock_docker_ctl, sample_services, sample_state):
        """Test that bottleneck detection uses ServiceTopology."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            agent = CoreAgent(
                monitor=mock_monitor,
                docker_ctl=mock_docker_ctl,
                services=sample_services,
                interval=5,
                use_rl=False,
                mode="dry-run",
                enable_metrics=False,
            )

        states = {"database": sample_state, "api": sample_state, "web-app": sample_state}

        with patch.object(agent.topology, "record_tick") as mock_record:
            with patch.object(agent.topology, "detect_bottleneck", return_value="database") as mock_detect:
                bottleneck = agent._detect_bottleneck(states)

        mock_record.assert_called_once_with(states)
        mock_detect.assert_called_once()
        assert bottleneck == "database"


class TestCommandGeneration:
    """Test command generation."""

    def test_generate_commands_for_all_services(self, mock_monitor, mock_docker_ctl, sample_services, sample_state, mock_service_configs):
        """Test that commands are generated for all services."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        states = {"db": sample_state, "api": sample_state, "web-app": sample_state}
        commands = agent._generate_commands(bottleneck=None, states=states)

        assert len(commands) == len(sample_services)
        for cmd in commands:
            assert cmd.target_service in sample_services
            assert cmd.validate()

    def test_generate_commands_with_bottleneck(self, mock_monitor, mock_docker_ctl, sample_services, sample_state, mock_service_configs):
        """Test command generation when bottleneck exists."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        states = {"db": sample_state, "api": sample_state, "web-app": sample_state}
        commands = agent._generate_commands(bottleneck="db", states=states)

        # Find command for bottleneck service
        bottleneck_cmd = next(cmd for cmd in commands if cmd.target_service == "db")
        assert bottleneck_cmd.command_type == CommandType.EMERGENCY_PRIORITY
        assert bottleneck_cmd.bottleneck_service == "db"


class TestBottleneckFilter:
    """Test bottleneck filtering."""

    def test_filter_blocks_downstream_scale_up(self, mock_monitor, mock_docker_ctl, sample_services, sample_state, mock_service_configs):
        """Test that scale_up is blocked for downstream services."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        states = {"db": sample_state, "api": sample_state, "web-app": sample_state}

        # Mock topology to indicate api is downstream of db
        with patch.object(agent.topology, "is_downstream_of_bottleneck", return_value=True):
            filtered_action, action_name = agent._apply_bottleneck_filter(
                service="api",
                action=1,  # scale_up
                bottleneck="db",
                states=states,
            )

        assert filtered_action == 0  # no_op
        assert "downstream_blocked" in action_name

    def test_filter_blocks_scale_down_when_upstream_recovering(self, mock_monitor, mock_docker_ctl, sample_services, sample_state, mock_service_configs):
        """Test that scale_down is blocked when upstream is recovering."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        states = {"db": sample_state, "api": sample_state, "web-app": sample_state}

        # Mock topology to indicate upstream is recovering
        with patch.object(agent.topology, "is_upstream_recovering", return_value=True):
            filtered_action, action_name = agent._apply_bottleneck_filter(
                service="api",
                action=2,  # scale_down
                bottleneck=None,
                states=states,
            )

        assert filtered_action == 0  # no_op
        assert "upstream_recovering" in action_name


class TestCommandDispatch:
    """Test command dispatch."""

    def test_dispatch_command_success(self, mock_monitor, mock_docker_ctl, sample_services, mock_service_configs):
        """Test successful command dispatch."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            with patch("orchestrator.core_agent.SERVICE_CONFIGS", mock_service_configs):
                agent = CoreAgent(
                    monitor=mock_monitor,
                    docker_ctl=mock_docker_ctl,
                    services=sample_services,
                    interval=5,
                    use_rl=False,
                    mode="dry-run",
                    enable_metrics=False,
                )

        cmd = OrchestrationCommand(
            target_service="db",
            command_type=CommandType.NORMAL_OPERATION,
        )

        # Mock the service agent's _tick method
        with patch.object(agent.service_agents["db"], "_tick"):
            result = agent._dispatch_command(cmd)

        assert result.service == "db"
        assert result.success is True

    def test_dispatch_command_agent_not_found(self, mock_monitor, mock_docker_ctl, sample_services):
        """Test command dispatch when agent not found."""
        with patch("orchestrator.core_agent.start_metrics_server"):
            agent = CoreAgent(
                monitor=mock_monitor,
                docker_ctl=mock_docker_ctl,
                services=sample_services,
                interval=5,
                use_rl=False,
                mode="dry-run",
                enable_metrics=False,
            )

        cmd = OrchestrationCommand(
            target_service="nonexistent",
            command_type=CommandType.NORMAL_OPERATION,
        )

        result = agent._dispatch_command(cmd)

        assert result.service == "nonexistent"
        assert result.success is False
        assert "agent_not_found" in result.blocked_reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
