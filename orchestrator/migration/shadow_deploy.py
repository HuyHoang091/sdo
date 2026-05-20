import time
import threading
import logging
from orchestrator.config import AGENT_MODE
from orchestrator.agent import MultiServiceAgent
from orchestrator.core_agent import CoreAgent
from orchestrator.monitor import PrometheusMonitor
from orchestrator.docker_ctl import DockerController

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("shadow_deploy")

def run_legacy_agent():
    logger.info("Starting V1 MultiServiceAgent (Live/Shadow Mode)...")
    # Khởi tạo các module phụ thuộc
    monitor = PrometheusMonitor()
    docker_ctl = DockerController()
    
    agent_v1 = MultiServiceAgent(
        monitor=monitor, 
        docker_ctl=docker_ctl, 
        services=["web-app", "api"]
    )
    # Ép V1 chạy dry-run nếu V2 đang chạy live để tránh đụng độ Docker
    agent_v1.mode = "dry-run" if AGENT_MODE == "live" else AGENT_MODE
    agent_v1.run()

def run_hierarchical_agent():
    logger.info("Starting V2 CoreAgent (Hierarchical Mode)...")
    # Khởi tạo các module phụ thuộc
    monitor = PrometheusMonitor()
    docker_ctl = DockerController()
    
    agent_v2 = CoreAgent(
        monitor=monitor, 
        docker_ctl=docker_ctl, 
        services=["web-app", "api"]
    )
    agent_v2.run()

if __name__ == "__main__":
    logger.info("=== STARTING SHADOW DEPLOYMENT ===")
    
    t1 = threading.Thread(target=run_legacy_agent)
    t2 = threading.Thread(target=run_hierarchical_agent)
    
    t1.start()
    time.sleep(2) # Offset slightly to avoid docker stat race conditions
    t2.start()
    
    try:
        t1.join()
        t2.join()
    except KeyboardInterrupt:
        logger.info("Shutting down both systems...")
