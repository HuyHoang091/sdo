import pytest
import time
import threading
from unittest.mock import patch, MagicMock

from orchestrator.core_agent import CoreAgent
from orchestrator.service_agent import ServiceAgent
from orchestrator.commands import CommandType

class TestHierarchicalIntegration:
    """
    Integration tests cho hệ thống Hierarchical Multi-Agent RL.
    Bao phủ các task 23.1 đến 23.6 trong specs.
    """
    
    @pytest.fixture
    def setup_system(self):
        with patch('orchestrator.docker_ctl.DockerController') as MockDocker:
            mock_docker = MockDocker.return_value
            mock_docker.get_service_stats.return_value = {
                "web-app": {"cpu": 50, "mem": 40, "containers": 2, "health_ratio": 1.0, "error_rate": 0.0, "response_time_ms": 150},
                "api": {"cpu": 85, "mem": 60, "containers": 2, "health_ratio": 0.9, "error_rate": 0.1, "response_time_ms": 500}
            }
            
            # Khởi tạo hệ thống
            core_agent = CoreAgent(services=["web-app", "api"])
            
            # Ghi đè Service Agents thực tế bằng mock để test giao tiếp
            core_agent.service_agents["web-app"].receive_command = MagicMock()
            core_agent.service_agents["api"].receive_command = MagicMock()
            
            yield core_agent, mock_docker

    def test_23_1_core_to_service_communication(self, setup_system):
        """Test Core Agent gửi lệnh xuống Service Agent thành công"""
        core_agent, _ = setup_system
        core_agent.service_agents["api"]._tick = MagicMock()
        
        # Mô phỏng tick của core
        core_agent._tick()
        
        # Kiểm tra xem receive_command có được gọi với đúng cấu trúc OrchestrationCommand không
        core_agent.service_agents["api"].receive_command.assert_called()
        command_arg = core_agent.service_agents["api"].receive_command.call_args[0][0]
        assert command_arg.target_service == "api"

    def test_23_2_docker_action_execution(self, setup_system):
        """Test hành động RL dịch thành Docker command thực tế"""
        core_agent, mock_docker = setup_system
        agent = core_agent.service_agents["web-app"]
        agent.docker_ctl = mock_docker
        
        # Bypass rl model, force action 1 (scale_up)
        with patch.object(agent, 'rl_env') as mock_env:
            agent.rl_env.take_action = MagicMock(return_value="scale_up")
            agent._tick()
            
            mock_docker.scale_service.assert_called_with("web-app", 3) # hiện tại 2 + 1 = 3

    def test_23_3_prometheus_metrics_export(self):
        """Test AgentMetrics cập nhật đúng trạng thái cho Prometheus"""
        from orchestrator.agent_metrics import AgentMetrics
        metrics = AgentMetrics()
        metrics.update_service_stats("api", {"cpu": 75, "mem": 50, "containers": 3, "health_ratio": 0.9})
        metrics.inc_action("api", "scale_up", False)
        
        # Verify qua raw client
        assert metrics.action_counter.labels(service="api", action="scale_up", dry_run="False")._value.get() == 1

    def test_23_4_graceful_shutdown(self, setup_system):
        """Test Core Agent shutdown an toàn"""
        core_agent, _ = setup_system
        core_agent.running = True
        
        # Giả lập SIGINT
        def trigger_shutdown():
            time.sleep(0.1)
            core_agent.running = False
            
        t = threading.Thread(target=trigger_shutdown)
        t.start()
        core_agent.run() # Nên thoát gracefully khi running = False
        t.join()
        assert core_agent.running is False

    def test_23_5_failover_scenarios(self, setup_system):
        """Test failover khi Core Agent chết -> Service Agent dùng standalone_mode"""
        core_agent, _ = setup_system
        service_agent = ServiceAgent("web-app", core_agent=None) # Mất kết nối
        
        assert service_agent.standalone_mode is True
        
        with patch.object(service_agent, 'run_standalone') as mock_standalone:
            service_agent._tick()
            mock_standalone.assert_called_once()

    def test_23_6_e2e_hierarchical_operation(self, setup_system):
        """End-to-End: Metrics -> Bottleneck -> Command -> Action"""
        core_agent, mock_docker = setup_system
        
        # Set API as bottleneck
        mock_docker.get_service_stats.return_value["api"]["cpu"] = 95
        mock_docker.get_service_stats.return_value["api"]["response_time_ms"] = 3000
        
        # Khôi phục agent thực để test luồng xuyên suốt
        core_agent.service_agents["api"] = ServiceAgent("api", core_agent=core_agent)
        core_agent.service_agents["web-app"] = ServiceAgent("web-app", core_agent=core_agent)
        
        with patch.object(core_agent.service_agents["web-app"].docker_ctl, 'scale_service') as mock_scale:
            # Tick core -> Detect bottleneck -> Issue SCALE_DOWN_ALLOWED / SCALE_UP_BLOCKED -> Tick services
            core_agent._tick()
            core_agent.service_agents["api"]._tick()
            core_agent.service_agents["web-app"]._tick()
            
            # Web app đang là downstream của API, do API bị nghẽn nên Web app sẽ KHÔNG bị scale up
            mock_scale.assert_not_called()
