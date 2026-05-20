"""
SDO Configuration - Cấu hình chung cho hệ thống
"""

import os
import logging
import yaml

# === Logging ===
LOG_LEVEL = os.getenv("SDO_LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sdo")

# === Prometheus ===
PROMETHEUS_URL = os.getenv("SDO_PROMETHEUS_URL", "http://localhost:9090")
PROMETHEUS_QUERY_ENDPOINT = f"{PROMETHEUS_URL}/api/v1/query"

# === Docker ===
COMPOSE_FILE = os.getenv("SDO_COMPOSE_FILE", "docker-compose.yml")
DEFAULT_SERVICE = os.getenv("SDO_SERVICE", "web-app")

# "nginx"   : (Cũ) Ghi đè nginx.conf. Gây mất config của người dùng.
# "nginx_fragment": (Cũ) Phức tạp khi scale up.
# "traefik" : Tự động gán label Traefik lên container qua Docker API.
# "dns"     : (Cũ) Ngắt mạng container. Gây rớt kết nối đang chạy.
# "signal"  : (Mới & Xịn) Bắn Signal SIGQUIT từ xa vào Container để Nginx bên trong tự sụp hầm (từ chối connect mới, làm nốt connect cũ) trước khi scale.
# "none"    : Tắt tự động Load Balancer (Đã config trực tiếp từ Docker Compose + Nginx DNS Realtime)
LB_TYPE = os.getenv("SDO_LB_TYPE", "signal").lower()

# === Thresholds (defaults, override per-service below) ===
CPU_HIGH = 80       # % - scale up nếu vượt
CPU_LOW = 20        # % - scale down nếu dưới
MEM_DANGER = 90     # % - kill & replace
MEM_WARNING = 80    # % - cảnh báo
CPU_IDEAL = 60      # % - mức CPU lý tưởng (dùng cho reward)
MIN_CONTAINERS = 1  # Tối thiểu
MAX_CONTAINERS = 10 # Tối đa
# Thresholds mới #####
RESPONSE_TIME_CRITICAL = 3000  # ms - response > 3s = có vấn đề
ERROR_RATE_CRITICAL = 0.3      # 30% request fail = nghiêm trọng
DISK_IO_DANGER = 85            # % disk usage

# === Health Probe (universal — chỉ cần container có HTTP port) ===
PROBE_PORT = int(os.getenv("SDO_PROBE_PORT", "80"))
PROBE_PATH = os.getenv("SDO_PROBE_PATH", "/")
PROBE_TIMEOUT = float(os.getenv("SDO_PROBE_TIMEOUT", "5.0"))
PROBE_INTERVAL = int(os.getenv("SDO_PROBE_INTERVAL", "3"))  # probe mỗi 3s

#####

# === Default Rollback Settings ===
ROLLBACK_WINDOW_SECONDS = int(os.getenv("SDO_ROLLBACK_WINDOW", "300"))
ROLLBACK_ERROR_THRESHOLD = float(os.getenv("SDO_ROLLBACK_ERROR_THRESHOLD", "0.1"))
ROLLBACK_LATENCY_FACTOR = float(os.getenv("SDO_ROLLBACK_LATENCY_FACTOR", "2.0"))
ROLLBACK_ACTION = os.getenv("SDO_ROLLBACK_ACTION", "rule-based")

# === Per-service Configuration ===
# Override defaults per service: min/max containers, thresholds, cooldown, rollback
SERVICE_CONFIGS = {
    "web-app": {
        "min_containers": 1,
        "max_containers": 8,
        "cpu_high": 80,
        "cpu_low": 20,
        "mem_danger": 90,
        "mem_warning": 80,
        "cooldown_seconds": 60,
        # Rollback per-service (override global defaults)
        "rollback_window_seconds": 300,
        "rollback_error_threshold": 0.1,   # Health drop > 10% → rollback
        "rollback_latency_factor": 2.0,    # CPU spike > 2x → rollback
        "rollback_action": "rule-based",   # "rule-based" | "disable" | "none"
    },
    # Ví dụ: background worker chịu lỗi cao hơn
    # "worker": {
    #     "min_containers": 1,
    #     "max_containers": 4,
    #     "cpu_high": 90,
    #     "cpu_low": 10,
    #     "mem_danger": 95,
    #     "mem_warning": 85,
    #     "cooldown_seconds": 120,
    #     "rollback_window_seconds": 600,   # 10 phút
    #     "rollback_error_threshold": 0.3,  # chịu 30% health drop
    #     "rollback_latency_factor": 3.0,
    #     "rollback_action": "none",        # không rollback, chỉ log
    # },
}

def _load_service_configs_from_file() -> None:
    """
    Nếu SDO_CONFIG trỏ tới file YAML, load và merge vào SERVICE_CONFIGS.
    Cấu trúc file mong muốn:
    services:
      my-app:
        min_containers: 1
        max_containers: 8
        cpu_high: 80
        cpu_low: 20
        mem_danger: 90
        mem_warning: 80
        cooldown_seconds: 60
        rollback_window_seconds: 300
        rollback_error_threshold: 0.1
        rollback_latency_factor: 2.0
        rollback_action: rule-based
    """
    path = os.getenv("SDO_CONFIG")
    if not path:
        return
    try:
        if not os.path.exists(path):
            logger.warning("SDO_CONFIG=%s nhưng file không tồn tại", path)
            return
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        services_cfg = data.get("services", {})
        if not isinstance(services_cfg, dict):
            logger.warning("File %s: 'services' phải là object", path)
            return
        for name, cfg in services_cfg.items():
            if not isinstance(cfg, dict):
                continue
            # merge: file ngoài override bản mặc định nếu trùng key
            base = SERVICE_CONFIGS.get(name, {})
            SERVICE_CONFIGS[name] = {**base, **cfg}
        logger.info("Loaded service configs from %s (services: %s)",
                    path, ", ".join(services_cfg.keys()))
    except Exception as e:
        logger.error("Không load được SDO_CONFIG=%s: %s", path, e)
# Gọi hàm này ngay sau khi khai báo SERVICE_CONFIGS
_load_service_configs_from_file()

_VALID_KEYS = {
    "min_containers",
    "max_containers",
    "cpu_high",
    "cpu_low",
    "mem_danger",
    "mem_warning",
    "cooldown_seconds",
    "rollback_window_seconds",
    "rollback_error_threshold",
    "rollback_latency_factor",
    "rollback_action",
    "rollback_min_observation",
    "tier", 
    "depends_on",
    "scalable",
    # DB Replica scaling (tùy chọn)
    "replica_enabled",
    "replica_service",
    "replica_min",
    "replica_max",
}
_REQUIRED_KEYS = {
    "min_containers",
    "max_containers",
    "cpu_high",
    "cpu_low",
    "mem_danger",
    "mem_warning",
}

def _validate_service_configs() -> None:
    for name, cfg in SERVICE_CONFIGS.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"SERVICE_CONFIGS['{name}'] must be a dict")

        # Thiếu field bắt buộc
        missing = _REQUIRED_KEYS - cfg.keys()
        if missing:
            raise ValueError(
                f"SERVICE_CONFIGS['{name}'] missing required keys: {sorted(missing)}"
            )

        # Field thừa / vô nghĩa
        extra = set(cfg.keys()) - _VALID_KEYS
        if extra:
            raise ValueError(
                f"SERVICE_CONFIGS['{name}'] has unknown keys: {sorted(extra)}"
            )

        # Ví dụ check đơn giản về kiểu/giá trị
        if cfg["min_containers"] < 1 or cfg["max_containers"] < cfg["min_containers"]:
            raise ValueError(
                f"SERVICE_CONFIGS['{name}']: invalid min/max_containers "
                f"({cfg['min_containers']}–{cfg['max_containers']})"
            )
        if "rollback_error_threshold" in cfg:
            if not (0 <= cfg["rollback_error_threshold"] <= 1):
                raise ValueError(
                    f"SERVICE_CONFIGS['{name}']: rollback_error_threshold must be 0–1"
                )

# Gọi ngay sau khi SERVICE_CONFIGS (và file YAML) được build xong:
# _load_service_configs_from_file()
_validate_service_configs()

def get_service_config(service: str) -> dict:
    """Lấy config cho service, fallback về defaults nếu chưa khai báo."""
    defaults = {
        "min_containers": MIN_CONTAINERS,
        "max_containers": MAX_CONTAINERS,
        "cpu_high": CPU_HIGH,
        "cpu_low": CPU_LOW,
        "mem_danger": MEM_DANGER,
        "mem_warning": MEM_WARNING,
        "cooldown_seconds": 60,
        "rollback_window_seconds": ROLLBACK_WINDOW_SECONDS,
        "rollback_error_threshold": ROLLBACK_ERROR_THRESHOLD,
        "rollback_latency_factor": ROLLBACK_LATENCY_FACTOR,
        "rollback_action": ROLLBACK_ACTION,
        "rollback_min_observation": 90,  # FIX BUG #6: giây chờ trước khi đánh giá rollback
    }
    cfg = SERVICE_CONFIGS.get(service, {})
    return {**defaults, **cfg}

# === RL Agent ===
STATE_DIM = 10       # Kích thước state vector
NUM_ACTIONS = 7     # Số lượng actions
AGENT_INTERVAL = int(os.getenv("SDO_AGENT_INTERVAL", "10"))  # Giây giữa mỗi lần check

# === Agent Modes ===
# "live"   : Agent thực thi action lên Docker
# "dry-run": Agent chỉ log action đề xuất, KHÔNG chạm Docker
# "shadow" : Alias của dry-run
AGENT_MODE = os.getenv("SDO_AGENT_MODE", "live")  # "live" | "dry-run" | "shadow"

# === Agent Metrics Exporter ===
AGENT_METRICS_PORT = int(os.getenv("SDO_METRICS_PORT", "9200"))

# === Multi-service Notes ===
# Mỗi service NÊN chạy 1 agent process riêng (1 process = 1 service).
# Nếu muốn chạy nhiều service trong 1 process, dùng MultiServiceAgent (sẽ share
# 1 metrics server). Port metrics cần khác nhau nếu chạy nhiều process.

# === Action Mapping ===
ACTIONS = {
    0: "no_op",
    1: "scale_up",
    2: "scale_down",
    3: "restart_unhealthy",
    4: "increase_cpu_limit",
    5: "increase_mem_limit",
    6: "kill_and_replace",
}

# === Chaos Training ===
CHAOS_TYPES = ["cpu_stress", "memory_leak", "kill_container", "network_delay", "disk_fill"]
CHAOS_EPISODE_STEPS = 13
# CHAOS_EPISODE_STEPS = 20       # Số bước trong 1 episode
CHAOS_STEP_INTERVAL = 5        # Giây giữa mỗi step
CHAOS_CLEANUP_WAIT = 10        # Giây chờ sau cleanup
DEFAULT_CHAOS_EPISODES = 50    # Số episode mặc định

# === Training ===
TRAINING_TIMESTEPS_OFFLINE = 50_000
TRAINING_TIMESTEPS_LIVE = 10_000
LEARNING_RATE = 3e-4
MODEL_DIR = "models"
MODEL_PATH = os.path.join(MODEL_DIR, "sdo_agent")
ONNX_MODEL_PATH = os.path.join(MODEL_DIR, "sdo_agent.onnx")
DATA_DIR = "training_data"
LOG_DIR = "logs"

# =============================================
# MULTI-SERVICE ORCHESTRATION (v2.0 upgrade)
# =============================================

# === Service Tiers (Priority Levels) ===
# Tier càng thấp = càng quan trọng = reward weight càng cao
SERVICE_TIERS = {
    1: {"name": "critical",  "reward_weight": 3.0},  # DB
    2: {"name": "important", "reward_weight": 2.0},  # API/Backend
    3: {"name": "standard",  "reward_weight": 1.0},  # Proxy/Frontend
}

# === Bottleneck Detection ===
BOTTLENECK_WINDOW_TICKS = 1         # Số tick liên tiếp để xác nhận bottleneck
BOTTLENECK_CPU_THRESHOLD = 70       # % CPU → stressed
BOTTLENECK_ERROR_THRESHOLD = 0.2    # error_rate → stressed
BOTTLENECK_LATENCY_MS = 2000        # ms response time → stressed
# Fast-path P0: tier-1 critical → confirm ngay lập tức
P0_IMMEDIATE_ERROR = 0.5            # error_rate > 50% → immediate
P0_IMMEDIATE_HEALTH = 0.5           # health_ratio < 50% → immediate

# === Multi-Service Agent ===
MULTI_STATE_DIM = 28                # Per-service obs dimension (10 local + 14 fleet + 4 topo)
MULTI_AGENT_INTERVAL = 5           # 15 Giây giữa mỗi tick (dài hơn single vì multi-probe)
MAX_CONCURRENT_PROBES = 5           # ThreadPool workers cho parallel probe
MULTI_MODEL_PATH = os.path.join(MODEL_DIR, "sdo_multi_agent")
MULTI_ONNX_PATH = os.path.join(MODEL_DIR, "sdo_multi_agent.onnx")

# === Host Overload Circuit Breaker ===
HOST_CPU_CRITICAL = int(os.getenv("SDO_HOST_CPU_CRITICAL", "90"))    # %
HOST_MEM_CRITICAL = int(os.getenv("SDO_HOST_MEM_CRITICAL", "90"))    # %
CIRCUIT_BREAKER_FRONTEND = os.getenv("SDO_CB_FRONTEND", "web-app")
CIRCUIT_BREAKER_RECOVERY_THRESHOLD = 70  # % — host < 70% = hết overload
