"""
SDO Monitor - Thu thập metrics từ Prometheus và tạo state vector cho RL.

Fixes applied:
- BUG-2: healthy_count giờ query Docker API thay vì hardcode = total
- BUG-3: print → logging
- WEAK-8: Assert state vector dimension
"""

import logging
import time
import requests
import numpy as np
from dataclasses import dataclass, asdict
from typing import Optional

from orchestrator.config import PROMETHEUS_QUERY_ENDPOINT, STATE_DIM

log = logging.getLogger("sdo.monitor")


@dataclass
class ServiceState:
    """Trạng thái của 1 service tại 1 thời điểm"""
    cpu_percent: float        # 0-100: CPU usage trung bình các container
    memory_percent: float     # 0-100: Memory usage trung bình
    memory_bytes: float       # Bytes: Raw memory usage
    network_rx_rate: float    # Bytes/s: Tốc độ nhận data
    container_count: int      # Tổng số container đang chạy
    restart_count: int        # Tổng số lần restart
    healthy_count: int        # Số container healthy
    host_cpu_available: float # 0-100: CPU còn trống trên host
    host_mem_available: float # 0-100: Memory còn trống trên host
    # NEW: 3 metrics cho network/disk/health
    response_time_ms: float    # Avg response time từ probe (ms)
    error_rate: float          # 0-1: tỷ lệ probe fail
    disk_io_percent: float     # 0-100: disk usage %

    def to_dict(self):
        return asdict(self)


class PrometheusMonitor:
    """Client query Prometheus và xây dựng state vector"""

    def __init__(self, prometheus_url: Optional[str] = None, docker_client=None):
        self.query_url = prometheus_url or PROMETHEUS_QUERY_ENDPOINT
        if not self.query_url.endswith("/api/v1/query"):
            self.query_url = f"{self.query_url}/api/v1/query"

        # Docker client cho health check (lazy init)
        self._docker_client = docker_client

        self._probe_source = None          # Cache monitoring container
        self._probe_source_time = 0
        self._probe_cache = {}             # {service: (response_time, error_rate, timestamp)}
        self._probe_cache_ttl = 10         # Chỉ probe lại sau 10s

    @property
    def docker_client(self):
        """Lazy-init Docker client để tránh crash nếu Docker không available"""
        if self._docker_client is None:
            try:
                import docker
                self._docker_client = docker.from_env()
            except Exception as e:
                log.warning("Cannot connect to Docker API: %s", e)
        return self._docker_client

    def query(self, promql: str) -> float:
        """
        Chạy 1 PromQL query, trả về giá trị scalar.
        Nếu có nhiều kết quả, lấy trung bình.
        """
        try:
            r = requests.get(self.query_url, params={"query": promql}, timeout=5)
            r.raise_for_status()
            data = r.json()

            if data.get("status") != "success":
                return 0.0

            results = data.get("data", {}).get("result", [])
            if not results:
                return 0.0

            # Nếu nhiều kết quả → lấy trung bình
            values = [float(r["value"][1]) for r in results if r.get("value")]
            return sum(values) / len(values) if values else 0.0

        except Exception as e:
            log.warning("Prometheus query failed: %s | query=%s", e, promql[:80])
            return 0.0

    def query_sum(self, promql: str) -> float:
        """Query và tổng hợp (sum) tất cả kết quả"""
        try:
            r = requests.get(self.query_url, params={"query": promql}, timeout=5)
            r.raise_for_status()
            results = r.json().get("data", {}).get("result", [])
            values = [float(r["value"][1]) for r in results if r.get("value")]
            return sum(values)
        except Exception:
            return 0.0

    def query_count(self, promql: str) -> int:
        """Query và đếm số kết quả"""
        try:
            r = requests.get(self.query_url, params={"query": promql}, timeout=5)
            r.raise_for_status()
            results = r.json().get("data", {}).get("result", [])
            return len(results)
        except Exception:
            return 0

    def _count_healthy_containers(self, service_name: str, total_count: int) -> int:
        """
        FIX BUG-2: Query Docker API để lấy số container healthy thực tế.
        Fallback về total_count nếu Docker không available hoặc container
        không có healthcheck configured.
        """
        if self.docker_client is None:
            return total_count

        try:
            containers = self.docker_client.containers.list(
                filters={"label": f"com.docker.compose.service={service_name}"}
            )
            healthy = 0
            has_healthcheck = False
            for c in containers:
                health_info = c.attrs.get("State", {}).get("Health", {})
                status = health_info.get("Status", "")
                if status:  # Container có healthcheck defined
                    has_healthcheck = True
                    if status == "healthy":
                        healthy += 1
                else:
                    # Container không có healthcheck → coi như healthy nếu running
                    if c.status == "running":
                        healthy += 1

            # Nếu không container nào có healthcheck → trả total (tương thích cũ)
            if not has_healthcheck:
                return total_count

            return healthy
        except Exception as e:
            log.debug("Docker health check failed, using total: %s", e)
            return total_count

    def _get_container_ids(self, service_name: str) -> list[str]:
        """
        Lấy danh sách container IDs cho service từ Docker API.
        Trả về short IDs (12 chars) dùng để match trong PromQL.
        """
        if self.docker_client is None:
            return []
        try:
            containers = self.docker_client.containers.list(
                filters={"label": f"com.docker.compose.service={service_name}"}
            )
            return [c.id[:12] for c in containers]
        except Exception as e:
            log.debug("Cannot list containers via Docker API: %s", e)
            return []

    def _get_docker_stats(self, service_name: str) -> tuple[float, float, int]:
        """
        Fallback: lấy CPU% và MEM% trực tiếp từ Docker API stats.
        Dùng trên Windows/WSL2 nơi cAdvisor không đọc được container metrics.
        Returns: (cpu_percent, mem_percent, container_count)
        """
        if self.docker_client is None:
            return 0.0, 0.0, 0
        try:
            containers = self.docker_client.containers.list(
                filters={"label": f"com.docker.compose.service={service_name}"}
            )
            if not containers:
                return 0.0, 0.0, 0

            total_cpu = 0.0
            total_mem = 0.0
            count = 0
            for c in containers:
                try:
                    stats = c.stats(stream=False)
                    # Memory percent (luôn có)
                    mem_usage = stats.get("memory_stats", {}).get("usage", 0)
                    mem_limit = stats.get("memory_stats", {}).get("limit", 1)
                    if mem_limit > 0:
                        total_mem += (mem_usage / mem_limit) * 100

                    # CPU percent — xử lý Windows Docker Desktop edge cases
                    cpu_stats = stats.get("cpu_stats", {})
                    precpu_stats = stats.get("precpu_stats", {})
                    
                    cpu_usage = cpu_stats.get("cpu_usage", {})
                    precpu_usage = precpu_stats.get("cpu_usage", {})
                    
                    total_usage = cpu_usage.get("total_usage", 0)
                    prev_total = precpu_usage.get("total_usage", 0)
                    cpu_delta = total_usage - prev_total
                    
                    system_usage = cpu_stats.get("system_cpu_usage", 0)
                    prev_system = precpu_stats.get("system_cpu_usage", 0)
                    system_delta = system_usage - prev_system
                    
                    # Windows Docker: system_delta có thể = 0, dùng online_cpus
                    percpu = cpu_usage.get("percpu_usage", [])
                    cpu_count = len(percpu) if percpu else cpu_usage.get("online_cpus", 1)
                    
                    if system_delta > 0 and cpu_delta > 0:
                        cpu_pct = (cpu_delta / system_delta) * cpu_count * 100
                        total_cpu += cpu_pct
                    elif total_usage > 0:
                        # Fallback: ước tính từ total_usage (không chính xác nhưng có giá trị)
                        total_cpu += 0.5  # Đánh dấu có activity
                    count += 1
                except Exception as e:
                    log.debug("Docker stats error for container %s: %s", c.id[:12], e)
                    continue
            avg_cpu = total_cpu / count if count > 0 else 0.0
            avg_mem = total_mem / count if count > 0 else 0.0
            return avg_cpu, avg_mem, count
        except Exception as e:
            log.debug("Docker stats fallback failed for %s: %s", service_name, e)
            return 0.0, 0.0, 0

######################################

    def _probe_containers(self, service_name: str) -> tuple[float, float]:
        """
        Probe cross-container: từ container A → container B qua mạng Docker.
        Traffic đi qua eth0 → tc delay có tác dụng → đo được latency thật.
        Khi chỉ có 1 container: probe localhost (fallback).
        """
        """Probe với cache — tránh exec_run mỗi tick."""
        import os
        if os.getenv("SDO_PROBE_ENABLED", "true").lower() != "true":
            return 0.0, 0.0
        # Cache hit? Trả kết quả cũ nếu chưa hết TTL
        cached = self._probe_cache.get(service_name)
        if cached:
            rt, er, ts = cached
            if time.time() - ts < self._probe_cache_ttl:
                return rt, er
        # Cache source container (tránh tìm lại mỗi lần)
        if self._probe_source is None or time.time() - self._probe_source_time > 60:
            self._probe_source = self._find_probe_source()
            self._probe_source_time = time.time()
        source = self._probe_source

        from orchestrator.config import PROBE_PORT, PROBE_PATH, PROBE_TIMEOUT

        ids = self._get_container_ids(service_name)
        if not ids:
            return 0.0, 0.0

        # Fallback: Nếu không tìm thấy monitoring container, dùng 1 web-app container
        if source is None:
            for cid in ids:
                try:
                    c = self.docker_client.containers.get(cid)
                    if c.status == 'running':
                        source = c
                        break
                except Exception:
                    pass

        if source is None:
            return 0.0, 1.0  # Không có container nào chạy

        total_time = 0.0
        errors = 0
        probed = 0

        for cid in ids:
            try:
                container = self.docker_client.containers.get(cid)

                # Xác định target: dùng IP nội bộ (cross-container) hoặc localhost (self)
                if cid == source.id:
                    # Self-probe: dùng localhost
                    target_host = "localhost"
                else:
                    # Cross-container: lấy IP qua Docker network
                    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
                    target_host = None
                    for net_info in networks.values():
                        ip = net_info.get("IPAddress", "")
                        if ip:
                            target_host = ip
                            break
                    if not target_host:
                        errors += 1
                        probed += 1
                        continue

                # Probe từ SOURCE → TARGET (sh -c wrapper cần cho shell redirect)
                start = time.time()
                exit_code, output = source.exec_run(
                    f"sh -c 'wget -q -O /dev/null --timeout={int(PROBE_TIMEOUT)} "
                    f"http://{target_host}:{PROBE_PORT}{PROBE_PATH} && echo OK || echo FAIL'"
                )
                elapsed_ms = (time.time() - start) * 1000

                if exit_code == 0 and b"OK" in output:
                    total_time += elapsed_ms
                else:
                    total_time += PROBE_TIMEOUT * 1000
                    errors += 1
                probed += 1
            except Exception:
                errors += 1
                probed += 1

        if probed == 0:
            return 0.0, 0.0

        # Tính metrics
        avg_time = total_time / probed
        error_rate = errors / probed

        self._probe_cache[service_name] = (avg_time, error_rate, time.time())

        # Nếu bị CPU stress, latency có thể tăng tự nhiên 1 chút (Vd 50-200ms) do web server phản hồi chậm,
        # nhưng ta đã loại trừ độ trễ cực cao (>1000ms) của process overhead bằng cách probe từ ngoài.
        return avg_time, error_rate

    def _find_probe_source(self):
        """Tìm 1 container monitoring làm probe source (cache lại)."""
        for svc in ["prometheus", "grafana", "node-exporter"]:
            try:
                monitors = self.docker_client.containers.list(
                    filters={"label": f"com.docker.compose.service={svc}", "status": "running"}
                )
                if monitors:
                    return monitors[0]
            except Exception:
                pass
        return None

    def _get_disk_io_percent(self, pql_filter: str) -> float:
        """Query disk usage % từ cAdvisor (universal cho mọi container)"""
        usage = self.query_sum(f'container_fs_usage_bytes{{{pql_filter},device!=""}}')
        limit = self.query_sum(f'container_fs_limit_bytes{{{pql_filter},device!=""}}')
        if limit > 0:
            return min(usage / limit * 100, 100.0)
        return 0.0

####################################

    def _build_promql_filter(self, service_name: str) -> str:
        """
        Xây dựng PromQL filter phù hợp với cAdvisor format.

        Trên Linux: cAdvisor export label `container_label_com_docker_compose_service`
        Trên Windows (Docker Desktop/WSL2): cAdvisor KHÔNG export labels,
        chỉ có `id="/docker/<container_id>"`.

        Strategy:
        1. Thử label-based filter trước (Linux — nhanh hơn)
        2. Nếu không có kết quả → dùng Docker API lấy container IDs → filter by id regex
        """
        # Cache: đã detect được filter mode chưa?
        # FIX BUG #7: Invalidate cache mỗi 5 phút
        if not hasattr(self, '_filter_mode'):
            self._filter_mode = None
            self._filter_mode_time = 0

        if self._filter_mode and (time.time() - self._filter_mode_time > 300):
            self._filter_mode = None  # Re-detect

        if self._filter_mode == "labels":
            return f'container_label_com_docker_compose_service="{service_name}"'

        if self._filter_mode == "ids":
            return self._build_id_filter(service_name)

        # Auto-detect: thử label-based trước
        label_filter = f'container_label_com_docker_compose_service="{service_name}"'
        test_count = self.query_count(f'container_last_seen{{{label_filter}}}')

        if test_count > 0:
            self._filter_mode = "labels"
            self._filter_mode_time = time.time()
            log.info("Using label-based PromQL filter (Linux mode)")
            return label_filter

        # Label không work → thử ID-based (Windows mode)
        id_filter = self._build_id_filter(service_name)
        if id_filter:
            test_count = self.query_count(f'container_last_seen{{{id_filter}}}')
            if test_count > 0:
                self._filter_mode = "ids"
                self._filter_mode_time = time.time()
                log.info("Using container ID-based PromQL filter (Windows mode)")
                return id_filter

        # Cả hai đều fail → return label filter (sẽ return 0)
        log.warning("No cAdvisor metrics found for '%s' via labels or IDs", service_name)
        return label_filter

    def _build_id_filter(self, service_name: str) -> str:
        """Build PromQL regex filter dựa trên container IDs từ Docker API"""
        ids = self._get_container_ids(service_name)
        if not ids:
            return ""
        # PromQL regex: id=~"/docker/abc123.*|/docker/def456.*"
        id_patterns = "|".join(f"/docker/{cid}.*" for cid in ids)
        return f'id=~"{id_patterns}"'

    def get_state(self, service_name: str) -> ServiceState:
        """
        Thu thập toàn bộ metrics của 1 service từ Prometheus.
        Sử dụng cAdvisor metrics cho container, Node Exporter cho host.
        Docker API cho healthy count (FIX BUG-2).

        Auto-detect PromQL filter: label-based (Linux) hoặc ID-based (Windows).
        """
        pql_filter = self._build_promql_filter(service_name)

        # --- Container metrics (cAdvisor) ---
        # CPU usage (cores/second) — giá trị tuyệt đối
        cpu_usage = self.query_sum(
            f'rate(container_cpu_usage_seconds_total{{{pql_filter}}}[1m])'
        )
        # CPU limit (cores) — tổng CPU limit của tất cả containers
        # container_spec_cpu_quota / container_spec_cpu_period = CPU limit in cores
        cpu_quota = self.query_sum(
            f'container_spec_cpu_quota{{{pql_filter}}}'
        )
        cpu_period = self.query(
            f'container_spec_cpu_period{{{pql_filter}}}'
        )
        if cpu_quota > 0 and cpu_period > 0:
            cpu_limit = cpu_quota / cpu_period  # Tổng CPU cores limit
            cpu_percent = (cpu_usage / cpu_limit * 100) if cpu_limit > 0 else 0.0
        else:
            # Fallback: nếu không có quota (unlimited) → dùng absolute
            cpu_percent = cpu_usage * 100
        cpu_percent = min(cpu_percent, 100.0)  # Cap ở 100%

        mem_bytes = self.query_sum(
            f'container_memory_usage_bytes{{{pql_filter}}}'
        )

        mem_limit = self.query_sum(
            f'container_spec_memory_limit_bytes{{{pql_filter}}}'
        )
        mem_percent = (mem_bytes / mem_limit * 100) if mem_limit > 0 else 0.0

        # Fallback: Windows/WSL2 — cAdvisor không đọc được container metrics
        if cpu_percent == 0.0 and mem_percent == 0.0:
            docker_cpu, docker_mem, docker_count = self._get_docker_stats(service_name)
            if docker_count > 0:
                cpu_percent = docker_cpu
                mem_percent = docker_mem
                log.debug("Using Docker API stats for %s (cAdvisor unavailable)", service_name)

        net_rx_rate = self.query_sum(
            f'rate(container_network_receive_bytes_total{{{pql_filter}}}[1m])'
        )

        # Đếm container: dùng Docker API (chính xác hơn Prometheus)
        container_ids = self._get_container_ids(service_name)
        container_count = len(container_ids) if container_ids else self.query_count(
            f'container_last_seen{{{pql_filter}}}'
        )

        # FIX BUG-2: Query Docker API cho healthy count thực tế
        healthy_count = self._count_healthy_containers(service_name, container_count)

        # --- Host metrics (Node Exporter) ---
        host_cpu_used = self.query(
            '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)'
        )
        host_cpu_available = max(0, 100 - host_cpu_used)

        host_mem_available = self.query(
            '(node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100'
        )

        # FIX BUG #8: Query restart_count từ Prometheus thay vì hardcode 0
        restart_count_val = self.query_sum(
            f'container_restart_count{{{pql_filter}}}'
        )

        #####
        response_time_ms, error_rate = self._probe_containers(service_name)
        disk_io_percent = self._get_disk_io_percent(pql_filter)
        #####

        return ServiceState(
            cpu_percent=min(cpu_percent, 100),
            memory_percent=min(mem_percent, 100),
            memory_bytes=mem_bytes,
            network_rx_rate=net_rx_rate,
            container_count=max(container_count, 0),
            restart_count=int(restart_count_val),
            healthy_count=max(healthy_count, 0),
            host_cpu_available=host_cpu_available,
            host_mem_available=host_mem_available,
            ####
            response_time_ms=response_time_ms,
            error_rate=error_rate,
            disk_io_percent=disk_io_percent,
        )

    def to_vector(self, state: ServiceState) -> np.ndarray:
        """
        Normalize state thành vector 10 chiều [0, 1] cho RL agent.

        Dims:
          0: cpu_percent / 100
          1: memory_percent / 100
          2: network_rx_rate (capped at 10MB/s)
          3: container_count / MAX_CONTAINERS
          4: health_ratio (healthy / total)
          5: host_cpu_available / 100
          6: host_mem_available / 100
          7: response_time_ms / RESPONSE_TIME_CRITICAL
          8: error_rate (0-1)
          9: disk_io_percent / 100
        """
        from orchestrator.config import MAX_CONTAINERS, RESPONSE_TIME_CRITICAL

        health_ratio = (
            state.healthy_count / state.container_count
            if state.container_count > 0 else 1.0
        )

        vec = np.array([
            np.clip(state.cpu_percent / 100, 0, 1),
            np.clip(state.memory_percent / 100, 0, 1),
            np.clip(state.network_rx_rate / 1e7, 0, 1),  # 10MB/s = 1.0
            np.clip(state.container_count / MAX_CONTAINERS, 0, 1),
            np.clip(health_ratio, 0, 1),
            np.clip(state.host_cpu_available / 100, 0, 1),
            np.clip(state.host_mem_available / 100, 0, 1),
            # NEW: 3 dims
            np.clip(state.response_time_ms / RESPONSE_TIME_CRITICAL, 0, 1),
            np.clip(state.error_rate, 0, 1),
            np.clip(state.disk_io_percent / 100, 0, 1),
        ], dtype=np.float32)

        # FIX BUG #9: Dùng if/raise thay vì assert (để không bị disable bởi -O flag)
        if vec.shape != (STATE_DIM,):
            raise ValueError(
                f"State vector dimension mismatch: expected {STATE_DIM}, got {vec.shape[0]}. "
                f"Update STATE_DIM in config.py if you changed the vector."
            )

        return vec

    def print_state(self, service_name: str):
        """In state ra console (dùng logging)"""
        state = self.get_state(service_name)
        vec = self.to_vector(state)
        log.info("=" * 50)
        log.info("Service: %s", service_name)
        log.info("  CPU:        %6.1f%%", state.cpu_percent)
        log.info("  Memory:     %6.1f%% (%.1f MB)", state.memory_percent, state.memory_bytes / 1e6)
        log.info("  Network RX: %.1f KB/s", state.network_rx_rate / 1e3)
        log.info("  Containers: %d (healthy: %d)", state.container_count, state.healthy_count)
        log.info("  Host CPU:   %.1f%% available", state.host_cpu_available)
        log.info("  Host MEM:   %.1f%% available", state.host_mem_available)
        log.info("  Vector:     %s", vec)
        return state

    # =============================================
    # MULTI-SERVICE METHODS (v2.0 upgrade)
    # =============================================

    def get_multi_state(self, services: list) -> dict:
        """
        Thu thập state TẤT CẢ services SONG SONG.
        Dùng ThreadPoolExecutor để probe N services cùng lúc.
        Returns: {service_name: ServiceState}
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from orchestrator.config import MAX_CONCURRENT_PROBES

        results = {}
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROBES) as pool:
            futures = {pool.submit(self.get_state, svc): svc for svc in services}
            for future in as_completed(futures):
                svc = futures[future]
                try:
                    results[svc] = future.result(timeout=30)
                except Exception as e:
                    log.warning("Failed to probe %s: %s", svc, e)
                    # Tạo zero-state để tránh crash
                    results[svc] = ServiceState(
                        cpu_percent=0, memory_percent=0, memory_bytes=0,
                        network_rx_rate=0, container_count=0, restart_count=0,
                        healthy_count=0, host_cpu_available=0, host_mem_available=0,
                        response_time_ms=0, error_rate=1.0, disk_io_percent=0,
                    )
        return results

    def to_multi_vector(
        self,
        service: str,
        local_state: "ServiceState",
        all_states: dict,
        topology,
    ) -> np.ndarray:
        """
        Tạo 28-dim per-service obs vector, có fleet context.

        Layout (28 dims):
         [0:10]  Local metrics     — 10 dims giống to_vector()
         [10:14] Fleet CPU stats   — avg, max, min, std (ALL services)
         [14:18] Fleet MEM stats   — avg, max, min, std (ALL services)
         [18:20] Fleet health      — avg health ratio, % services stressed
         [20:22] Fleet scale       — total containers/MAX, avg containers/MAX
         [22:24] Fleet network     — avg response_time norm, avg error_rate
         [24:28] Topology context  — tier, dep_health, is_bottleneck, cascade_risk
        """
        from orchestrator.config import MAX_CONTAINERS, RESPONSE_TIME_CRITICAL, MULTI_STATE_DIM

        # [0:10] Local (reuse existing to_vector logic)
        local_vec = self.to_vector(local_state)

        # Collect fleet metrics
        cpus = [s.cpu_percent for s in all_states.values()]
        mems = [s.memory_percent for s in all_states.values()]
        healths = [s.healthy_count / max(s.container_count, 1) for s in all_states.values()]
        stressed_count = sum(
            1 for s in all_states.values()
            if s.cpu_percent > 70 or s.error_rate > 0.2 or s.response_time_ms > 2000
        )

        # [10:14] Fleet CPU
        fleet_cpu = np.array([
            np.mean(cpus) / 100,
            np.max(cpus) / 100,
            np.min(cpus) / 100,
            np.std(cpus) / 100 if len(cpus) > 1 else 0,
        ], dtype=np.float32)

        # [14:18] Fleet MEM
        fleet_mem = np.array([
            np.mean(mems) / 100,
            np.max(mems) / 100,
            np.min(mems) / 100,
            np.std(mems) / 100 if len(mems) > 1 else 0,
        ], dtype=np.float32)

        # [18:20] Fleet health
        fleet_health = np.array([
            np.mean(healths),
            stressed_count / max(len(all_states), 1),
        ], dtype=np.float32)

        # [20:22] Fleet scale
        total_cnt = sum(s.container_count for s in all_states.values())
        avg_cnt = total_cnt / max(len(all_states), 1)
        fleet_scale = np.array([
            np.clip(total_cnt / (MAX_CONTAINERS * len(all_states)), 0, 1),
            np.clip(avg_cnt / MAX_CONTAINERS, 0, 1),
        ], dtype=np.float32)

        # [22:24] Fleet network
        avg_rt = np.mean([s.response_time_ms for s in all_states.values()])
        avg_err = np.mean([s.error_rate for s in all_states.values()])
        fleet_net = np.array([
            np.clip(avg_rt / RESPONSE_TIME_CRITICAL, 0, 1),
            np.clip(avg_err, 0, 1),
        ], dtype=np.float32)

        # [24:28] Topology features
        topo_features = topology.to_topology_features(service, all_states)

        # Concat
        vec = np.concatenate([
            local_vec,       # 10
            fleet_cpu,       # 4
            fleet_mem,       # 4
            fleet_health,    # 2
            fleet_scale,     # 2
            fleet_net,       # 2
            topo_features,   # 4
        ])  # Total = 28

        vec = np.clip(vec, 0, 1).astype(np.float32)

        if vec.shape != (MULTI_STATE_DIM,):
            raise ValueError(
                f"Multi-state vector mismatch: expected {MULTI_STATE_DIM}, got {vec.shape[0]}"
            )
        return vec
