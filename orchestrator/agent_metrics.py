"""
SDO Agent Metrics - Export metrics của agent sang Prometheus.

Architecture:
  AgentMetrics là singleton — 1 instance dùng cho toàn bộ agent process.
  Nếu chạy nhiều service trong 1 process, tất cả share cùng 1 AgentMetrics
  và 1 metrics server (port duy nhất). Labels {service=...} phân biệt.
  Nếu chạy mỗi service 1 process riêng, dùng port khác nhau via SDO_METRICS_PORT.

Metrics exported:
  sdo_agent_actions_total{action, service}         : Counter
  sdo_agent_reward_average{service}                : Gauge
  sdo_agent_reward_total{service}                  : Counter
  sdo_agent_fallback_rulebased_total{service}      : Counter
  sdo_agent_errors_total{service}                  : Counter
  sdo_agent_rollback_total{service}                : Counter
  sdo_agent_cooldown_blocked_total{service}        : Counter
  sdo_agent_dryrun_total{service}                  : Counter
  sdo_agent_mode{service, mode}                    : Gauge
  sdo_agent_containers_count{service}              : Gauge
  sdo_agent_cpu_percent{service}                   : Gauge
  sdo_agent_memory_percent{service}                : Gauge
  sdo_agent_health_ratio{service}                  : Gauge
  sdo_agent_uptime_seconds                         : Gauge
"""

import time
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict

log = logging.getLogger("sdo.metrics")

# === Singleton ===
_global_metrics: "AgentMetrics | None" = None
_metrics_server_started = False


def get_metrics() -> "AgentMetrics":
    """
    Lấy singleton AgentMetrics.
    Nhiều service trong 1 process sẽ share cùng instance này.
    """
    global _global_metrics
    if _global_metrics is None:
        _global_metrics = AgentMetrics()
    return _global_metrics


class AgentMetrics:
    """Thu thập và export metrics của agent dưới dạng Prometheus text format"""

    def __init__(self):
        self._start_time = time.time()

        # Counters
        self.actions_total: dict[str, int] = defaultdict(int)
        self.reward_total: dict[str, float] = defaultdict(float)
        self.reward_count: dict[str, int] = defaultdict(int)
        self.fallback_total: dict[str, int] = defaultdict(int)
        self.errors_total: dict[str, int] = defaultdict(int)
        self.rollback_total: dict[str, int] = defaultdict(int)
        self.cooldown_blocked_total: dict[str, int] = defaultdict(int)
        self.dryrun_total: dict[str, int] = defaultdict(int)

        # Gauges
        self.current_mode: dict[str, str] = {}
        self.current_containers: dict[str, int] = {}
        self.current_cpu: dict[str, float] = {}
        self.current_memory: dict[str, float] = {}
        self.current_health_ratio: dict[str, float] = {}

    def record_action(self, service: str, action_name: str):
        self.actions_total[f"{service}:{action_name}"] += 1

    def record_reward(self, service: str, reward: float):
        self.reward_total[service] += reward
        self.reward_count[service] += 1

    def record_fallback(self, service: str):
        self.fallback_total[service] += 1

    def record_error(self, service: str):
        self.errors_total[service] += 1

    def record_rollback(self, service: str):
        self.rollback_total[service] += 1

    def record_cooldown_blocked(self, service: str):
        self.cooldown_blocked_total[service] += 1

    def record_dryrun(self, service: str):
        self.dryrun_total[service] += 1

    def update_gauges(self, service: str, mode: str, containers: int,
                      cpu: float, memory: float, health_ratio: float):
        self.current_mode[service] = mode
        self.current_containers[service] = containers
        self.current_cpu[service] = cpu
        self.current_memory[service] = memory
        self.current_health_ratio[service] = health_ratio

    def to_prometheus(self) -> str:
        """Generate Prometheus text exposition format"""
        lines = []

        def _counter(name, help_text, data, label_fn=None):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            for key, val in sorted(data.items()):
                labels = label_fn(key) if label_fn else f'{{service="{key}"}}'
                lines.append(f"{name}{labels} {val}")
            lines.append("")

        def _gauge(name, help_text, data, fmt=".2f"):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            for key, val in sorted(data.items()):
                lines.append(f'{name}{{service="{key}"}} {val:{fmt}}')
            lines.append("")

        # Uptime
        uptime = time.time() - self._start_time
        lines.append("# HELP sdo_agent_uptime_seconds Agent uptime")
        lines.append("# TYPE sdo_agent_uptime_seconds gauge")
        lines.append(f"sdo_agent_uptime_seconds {uptime:.1f}")
        lines.append("")

        # Actions
        _counter("sdo_agent_actions_total", "Total actions",
                 self.actions_total,
                 lambda k: f'{{service="{k.split(":", 1)[0]}",action="{k.split(":", 1)[1]}"}}')

        # Reward
        avg_reward = {}
        for s in self.reward_count:
            avg_reward[s] = self.reward_total[s] / max(self.reward_count[s], 1)
        _gauge("sdo_agent_reward_average", "Average reward per step", avg_reward, ".4f")
        # FIX BUG #4: reward_total có thể giảm → dùng gauge thay vì counter
        _gauge("sdo_agent_reward_total", "Cumulative reward (can decrease)",
               self.reward_total, ".4f")

        # Counters
        _counter("sdo_agent_fallback_rulebased_total", "Fallback to rule-based", self.fallback_total)
        _counter("sdo_agent_errors_total", "Agent errors", self.errors_total)
        _counter("sdo_agent_rollback_total", "Rollback triggers", self.rollback_total)
        _counter("sdo_agent_cooldown_blocked_total", "Actions blocked by cooldown", self.cooldown_blocked_total)
        _counter("sdo_agent_dryrun_total", "Dry-run suggestions", self.dryrun_total)

        # Mode gauge
        lines.append("# HELP sdo_agent_mode Agent mode (1=rl, 0.5=rules, 0=disabled)")
        lines.append("# TYPE sdo_agent_mode gauge")
        mode_values = {"rl": 1.0, "rules": 0.5, "disabled": 0.0}
        for service, mode in sorted(self.current_mode.items()):
            val = mode_values.get(mode, 0)
            lines.append(f'sdo_agent_mode{{service="{service}",mode="{mode}"}} {val}')
        lines.append("")

        # Gauges
        _gauge("sdo_agent_containers_count", "Container count", self.current_containers, ".0f")
        _gauge("sdo_agent_cpu_percent", "CPU percent", self.current_cpu)
        _gauge("sdo_agent_memory_percent", "Memory percent", self.current_memory)
        _gauge("sdo_agent_health_ratio", "Health ratio", self.current_health_ratio, ".4f")

        return "\n".join(lines)


class MetricsHTTPHandler(BaseHTTPRequestHandler):
    metrics: AgentMetrics = None

    def do_GET(self):
        try:
            if self.path == "/metrics":
                body = self.metrics.to_prometheus().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # Client ngắt kết nối sớm (Prometheus scrape timeout), bỏ qua an toàn
        except Exception as e:
            log.debug("Metrics HTTP handler error: %s", e)

    def log_message(self, format, *args):
        pass  # Suppress access logs


def start_metrics_server(metrics: AgentMetrics, port: int = 9200) -> threading.Thread:
    """
    Khởi chạy HTTP server trên background thread. Chỉ chạy 1 lần.
    Gọi lại sẽ bỏ qua (singleton pattern, an toàn cho multi-service).
    """
    global _metrics_server_started
    if _metrics_server_started:
        log.debug("Metrics server already running, skipping")
        return None

    MetricsHTTPHandler.metrics = metrics
    server = HTTPServer(("0.0.0.0", port), MetricsHTTPHandler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _metrics_server_started = True
    log.info("Agent metrics server started on :%d/metrics", port)
    return thread
