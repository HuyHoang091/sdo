"""
SDO Main - CLI Entry Point

Usage:
  python -m orchestrator.main monitor                 # Xem metrics live
  python -m orchestrator.main chaos [type]            # Inject chaos
  python -m orchestrator.main collect [N]             # Thu thập N episodes
  python -m orchestrator.main train-offline <file>    # Train từ data file
  python -m orchestrator.main train-live              # Fine-tune live
  python -m orchestrator.main agent                   # Chạy agent live (RL)
  python -m orchestrator.main agent --rules           # Agent rule-based
  python -m orchestrator.main agent --dry-run         # Shadow mode (chỉ log)
  python -m orchestrator.main agent --dry-run --rules # Shadow + rule-based
  python -m orchestrator.main export-onnx             # Export SB3 model → ONNX
  python -m orchestrator.main status                  # Xem trạng thái containers
  python -m orchestrator.main dashboard               # Chạy dashboard UI/API

  python -m orchestrator.main multi-agent              # Multi-service agent
  python -m orchestrator.main multi-agent --rules      # Rule-based multi
  python -m orchestrator.main multi-agent --dry-run    # Shadow multi
  python -m orchestrator.main topology                 # Xem dependency graph
  python -m orchestrator.main collect-multi [N]          # Thu thập multi-service chaos data
  python -m orchestrator.main train-offline-multi <file> # Train multi-service model
"""

import sys
import time
from orchestrator.config import DEFAULT_SERVICE, DEFAULT_CHAOS_EPISODES


def cmd_monitor(service: str):
    """Xem metrics từ Prometheus liên tục"""
    from orchestrator.monitor import PrometheusMonitor
    monitor = PrometheusMonitor()
    print(f"📊 Monitoring service: {service} (Ctrl+C to stop)\n")
    try:
        while True:
            monitor.print_state(service)
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n🛑 Stopped")


def cmd_chaos(service: str, chaos_type: str = None):
    """Inject chaos 1 lần"""
    from orchestrator.docker_ctl import DockerController
    from orchestrator.chaos import ChaosInjector

    docker_ctl = DockerController()
    chaos = ChaosInjector(docker_ctl)

    if chaos_type and hasattr(chaos, chaos_type):
        getattr(chaos, chaos_type)(service)
    else:
        chaos.random_chaos(service)

    print(f"\n🔥 Active: {chaos.get_status()}")
    print("   Run 'cleanup' to stop, or wait...")


def cmd_collect(service: str, num_episodes: int):
    """Chạy chaos campaign, thu thập data"""
    from orchestrator.monitor import PrometheusMonitor
    from orchestrator.docker_ctl import DockerController
    from orchestrator.chaos import ChaosInjector
    from orchestrator.train import ChaosDataCollector

    monitor = PrometheusMonitor()
    docker_ctl = DockerController()
    chaos = ChaosInjector(docker_ctl)
    collector = ChaosDataCollector(monitor, docker_ctl, chaos, service)
    collector.run_campaign(num_episodes)


def cmd_train_offline(data_path: str):
    """Train PPO từ collected data"""
    from orchestrator.train import train_offline
    train_offline(data_path)


def cmd_train_live(service: str):
    """Fine-tune PPO live"""
    from orchestrator.train import train_live
    train_live(service)


def cmd_agent(service: str, use_rl: bool = True, dry_run: bool = False):
    """Chạy live agent"""
    from orchestrator.monitor import PrometheusMonitor
    from orchestrator.docker_ctl import DockerController
    from orchestrator.agent import LiveAgent

    monitor = PrometheusMonitor()
    docker_ctl = DockerController()
    mode = "dry-run" if dry_run else "live"
    agent = LiveAgent(monitor, docker_ctl, service, use_rl=use_rl, mode=mode)
    agent.run()


def cmd_status(service: str):
    """Xem trạng thái containers"""
    from orchestrator.docker_ctl import DockerController
    from orchestrator.config import get_service_config
    docker_ctl = DockerController()
    cfg = get_service_config(service)
    containers = docker_ctl.get_containers_info(service)
    print(f"\n🐳 Service: {service}")
    print(f"   Containers: {len(containers)} (min={cfg['min_containers']}, max={cfg['max_containers']})")
    print(f"   Cooldown: {cfg['cooldown_seconds']}s\n")
    for c in containers:
        health_icon = {"healthy": "🟢", "unhealthy": "🔴", "starting": "🟡"}.get(c["health"], "⚪")
        print(f"   {health_icon} {c['id']} | {c['name']} | {c['status']} | {c['health']}")


def cmd_dashboard():
    """Chạy dashboard UI/API (FastAPI + Uvicorn)."""
    import os
    import uvicorn

    port = int(os.getenv("SDO_DASHBOARD_PORT", "9300"))
    # Import tại runtime để tránh load FastAPI khi không dùng
    from orchestrator.ui.dashboard import app

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

def cmd_multi_agent(use_rl: bool = True, dry_run: bool = False):
    """Chạy multi-service agent."""
    from orchestrator.monitor import PrometheusMonitor
    from orchestrator.docker_ctl import DockerController
    from orchestrator.core_agent import CoreAgent
    from orchestrator.config import SERVICE_CONFIGS

    monitor = PrometheusMonitor()
    docker_ctl = DockerController()

    # Lấy tất cả services có tier config
    services = [s for s, cfg in SERVICE_CONFIGS.items() if "tier" in cfg]
    if not services:
        print("❌ Không có service nào có 'tier' config.")
        print("   Thêm 'tier' và 'depends_on' vào SERVICE_CONFIGS hoặc sdo.yml")
        return

    mode = "dry-run" if dry_run else "live"
    agent = CoreAgent(
        monitor, docker_ctl, services,
        use_rl=use_rl, mode=mode,
    )
    agent.run()


def cmd_topology():
    """In dependency graph."""
    from orchestrator.topology import ServiceTopology
    from orchestrator.config import SERVICE_CONFIGS

    managed = {s: cfg for s, cfg in SERVICE_CONFIGS.items() if "tier" in cfg}
    if not managed:
        print("❌ Không có service nào có 'tier' config.")
        return

    topo = ServiceTopology(managed)
    topo.print_topology()

def cmd_collect_multi(num_episodes: int):
    """Chạy multi-service chaos campaign."""
    from orchestrator.monitor import PrometheusMonitor
    from orchestrator.docker_ctl import DockerController
    from orchestrator.chaos import ChaosInjector
    from orchestrator.train import MultiServiceChaosCollector
    from orchestrator.config import SERVICE_CONFIGS

    monitor = PrometheusMonitor()
    docker_ctl = DockerController()
    chaos = ChaosInjector(docker_ctl)

    services = [s for s, cfg in SERVICE_CONFIGS.items() if "tier" in cfg]
    if not services:
        print("❌ Không có service nào có 'tier' config.")
        return

    collector = MultiServiceChaosCollector(monitor, docker_ctl, chaos, services)
    collector.run_campaign(num_episodes)


def cmd_train_offline_multi(data_path: str):
    """Train PPO multi-service từ collected data."""
    from orchestrator.train import train_offline_multi
    train_offline_multi(data_path)

def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        return

    command = args[0]
    service = DEFAULT_SERVICE

    if "--service" in args:
        idx = args.index("--service")
        if idx + 1 < len(args):
            service = args[idx + 1]
            args = args[:idx] + args[idx + 2:]  # Bỏ --service X khỏi args

    if command == "monitor":
        cmd_monitor(service)

    elif command == "chaos":
        chaos_type = args[1] if len(args) > 1 else None
        cmd_chaos(service, chaos_type)

    elif command == "collect":
        n = int(args[1]) if len(args) > 1 else DEFAULT_CHAOS_EPISODES
        cmd_collect(service, n)

    elif command == "train-offline":
        if len(args) < 2:
            print("❌ Usage: train-offline <data_file.json>")
            return
        cmd_train_offline(args[1])

    elif command == "train-live":
        cmd_train_live(service)

    elif command == "agent":
        use_rl = "--rules" not in args
        dry_run = "--dry-run" in args or "--shadow" in args
        cmd_agent(service, use_rl=use_rl, dry_run=dry_run)

    elif command == "status":
        cmd_status(service)

    elif command == "dashboard":
        cmd_dashboard()

    elif command == "export-onnx":
        from orchestrator.export_onnx import export_to_onnx
        sb3_path = args[1] if len(args) > 1 else None
        onnx_path = args[2] if len(args) > 2 else None
        export_to_onnx(sb3_path, onnx_path)

    elif command == "collect-multi":
        n = int(args[1]) if len(args) > 1 else DEFAULT_CHAOS_EPISODES
        cmd_collect_multi(n)
    elif command == "train-offline-multi":
        if len(args) < 2:
            print("❌ Usage: train-offline-multi <data_file.json>")
            return
        cmd_train_offline_multi(args[1])

    elif command == "multi-agent":
        use_rl = "--rules" not in args
        dry_run = "--dry-run" in args or "--shadow" in args
        cmd_multi_agent(use_rl=use_rl, dry_run=dry_run)

    elif command == "topology":
        cmd_topology()

    else:
        print(f"❌ Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
