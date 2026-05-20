"""
SDO Service Topology — DAG dependency graph + temporal bottleneck detection.

Chức năng:
1. Build DAG từ SERVICE_CONFIGS (tier + depends_on)
2. Topological sort → thứ tự xử lý (tier 1 trước)
3. Temporal bottleneck detection (sliding window + P0 fast-path)
4. Topology features cho 28D obs vector
"""

import logging
import time
import numpy as np
from collections import deque
from typing import Optional

from orchestrator.config import (
    SERVICE_CONFIGS, SERVICE_TIERS,
    BOTTLENECK_WINDOW_TICKS, BOTTLENECK_CPU_THRESHOLD,
    BOTTLENECK_ERROR_THRESHOLD, BOTTLENECK_LATENCY_MS,
    P0_IMMEDIATE_ERROR, P0_IMMEDIATE_HEALTH,
    get_service_config,
)

log = logging.getLogger("sdo.topology")


class ServiceTopology:
    """
    DAG dependency graph giữa các services.
    Temporal bottleneck detection với sliding window + P0 fast-path.
    """

    def __init__(self, service_configs: dict = None):
        self._configs = service_configs or SERVICE_CONFIGS
        self._graph = {}        # {svc: {"tier": int, "depends_on": [], "downstream": []}}
        self._cached_bottleneck: Optional[str] = None
        self._history = deque(maxlen=BOTTLENECK_WINDOW_TICKS)
        self._build_graph()

    def _build_graph(self):
        """Xây dựng DAG từ config, auto-compute downstream."""
        for svc, cfg in self._configs.items():
            self._graph[svc] = {
                "tier": cfg.get("tier", 3),
                "depends_on": list(cfg.get("depends_on", [])),
                "downstream": [],
            }
        # Compute downstream (reverse of depends_on)
        for svc, info in self._graph.items():
            for upstream in info["depends_on"]:
                if upstream in self._graph:
                    self._graph[upstream]["downstream"].append(svc)

        log.info("Topology built: %d services", len(self._graph))
        for svc, info in sorted(self._graph.items(), key=lambda x: x[1]["tier"]):
            log.info("  [Tier %d] %s → depends_on=%s, downstream=%s",
                     info["tier"], svc, info["depends_on"], info["downstream"])

    @property
    def services(self) -> list:
        return list(self._graph.keys())

    # === Graph Queries ===

    def get_processing_order(self) -> list:
        """
        Topological sort thực sự (Kahn's algorithm).
        Trong cùng level, ưu tiên tier thấp trước.
        """
        in_degree = {svc: 0 for svc in self._graph}
        for svc, info in self._graph.items():
            for dep in info["depends_on"]:
                if dep in in_degree:
                    in_degree[svc] += 1
        # Seed: nodes không phụ thuộc ai, sort theo tier
        queue = sorted(
            [s for s, d in in_degree.items() if d == 0],
            key=lambda s: self._graph[s]["tier"]
        )
        result = []
        while queue:
            node = queue.pop(0)
            result.append(node)
            for downstream in self._graph[node]["downstream"]:
                in_degree[downstream] -= 1
                if in_degree[downstream] == 0:
                    queue.append(downstream)
                    queue.sort(key=lambda s: self._graph[s]["tier"])
        # Detect cycle: nếu result thiếu node → có circular dependency
        if len(result) < len(self._graph):
            missing = set(self._graph.keys()) - set(result)
            log.error("Circular dependency detected! Services not in order: %s", missing)
            # Fallback: append missing sorted by tier
            for svc in sorted(missing, key=lambda s: self._graph[s]["tier"]):
                result.append(svc)
        return result

    def get_tier(self, service: str) -> int:
        return self._graph.get(service, {}).get("tier", 3)

    def get_reward_weight(self, service: str) -> float:
        tier = self.get_tier(service)
        return SERVICE_TIERS.get(tier, {}).get("reward_weight", 1.0)

    def get_upstream(self, service: str) -> list:
        """Services mà service này phụ thuộc."""
        return self._graph.get(service, {}).get("depends_on", [])

    def get_downstream(self, service: str) -> list:
        """Services phụ thuộc vào service này."""
        return self._graph.get(service, {}).get("downstream", [])

    def get_cascade_impact(self, service: str) -> list:
        """DFS: tất cả downstream bị ảnh hưởng nếu service fail."""
        visited = set()
        stack = [service]
        while stack:
            s = stack.pop()
            for d in self.get_downstream(s):
                if d not in visited:
                    visited.add(d)
                    stack.append(d)
        return list(visited)

    # === Temporal Bottleneck Detection ===

    def record_tick(self, states: dict):
        """
        Ghi snapshot vào sliding window.
        states: {service_name: ServiceState}
        """
        snapshot = {}
        for svc, state in states.items():
            health_ratio = state.healthy_count / max(state.container_count, 1)
            snapshot[svc] = {
                "stressed": (
                    state.cpu_percent > BOTTLENECK_CPU_THRESHOLD
                    or state.error_rate > BOTTLENECK_ERROR_THRESHOLD
                    or state.response_time_ms > BOTTLENECK_LATENCY_MS
                ),
                "degraded": (
                    state.error_rate > 0.1
                    or state.response_time_ms > 1000
                    or health_ratio < 1.0
                ),
                "health_ratio": health_ratio,
                "error_rate": state.error_rate,
                "cpu": state.cpu_percent,
                "time": time.time(),
            }
        self._history.append(snapshot)
        # Detect ngay sau khi record
        self._cached_bottleneck = self._detect_bottleneck_internal()

    def detect_bottleneck(self) -> Optional[str]:
        """Trả về cached bottleneck (đã tính trong record_tick)."""
        return self._cached_bottleneck

    def _detect_bottleneck_internal(self) -> Optional[str]:
        """
        Logic detect:
        1. Fast-path P0: tier-1 error>0.5 hoặc health<0.5 → immediate
        2. Normal: ≥N ticks liên tiếp, stressed + downstream affected + temporal order
        """
        if not self._history:
            return None

        latest = self._history[-1]

        # === FAST-PATH P0: Tier 1 critical ===
        for svc in self.get_processing_order():
            if self.get_tier(svc) != 1:
                continue
            snap = latest.get(svc)
            if snap is None:
                continue
            if snap["error_rate"] > P0_IMMEDIATE_ERROR or snap["health_ratio"] < P0_IMMEDIATE_HEALTH:
                # Verify downstream IS affected
                has_affected_downstream = False
                for ds in self.get_downstream(svc):
                    ds_snap = latest.get(ds)
                    if ds_snap and ds_snap["degraded"]:
                        has_affected_downstream = True
                        break
                # Tier-1 extreme: confirm ngay cả khi downstream chưa bị
                # (vì downstream sắp bị cascade)
                if has_affected_downstream or snap["error_rate"] > P0_IMMEDIATE_ERROR:
                    log.warning("P0 FAST-PATH: %s is bottleneck (error=%.0f%%, health=%.0f%%)",
                                svc, snap["error_rate"] * 100, snap["health_ratio"] * 100)
                    return svc

        # === NORMAL PATH: Sliding window >=N ticks ===
        if len(self._history) < BOTTLENECK_WINDOW_TICKS:
            return None

        for svc in self.get_processing_order():
            # Check stressed in ALL recent N ticks
            all_stressed = all(
                tick.get(svc, {}).get("stressed", False)
                for tick in self._history
            )
            if not all_stressed:
                continue

            # Check downstream affected in ALL recent N ticks
            downstream = self.get_downstream(svc)
            if not downstream:
                continue  # Leaf node không có downstream → không phải bottleneck pattern

            downstream_affected = all(
                any(
                    tick.get(ds, {}).get("degraded", False)
                    for ds in downstream
                )
                for tick in self._history
            )
            if not downstream_affected:
                continue

            # Temporal order: svc stressed không muộn hơn downstream degraded
            # (check tick đầu tiên trong window)
            first_tick = self._history[0]
            svc_stressed_first = first_tick.get(svc, {}).get("stressed", False)
            if svc_stressed_first:
                log.warning("BOTTLENECK CONFIRMED: %s (tier %d, %d ticks, downstream=%s)",
                            svc, self.get_tier(svc), len(self._history), downstream)
                return svc

        return None

    def is_downstream_of_bottleneck(self, service: str) -> bool:
        """Check service nằm sau bottleneck hiện tại."""
        bn = self._cached_bottleneck
        if bn is None:
            return False
        return service in self.get_cascade_impact(bn)

    def is_upstream_recovering(self, service: str, states: dict) -> bool:
        """
        Check upstream đang recovering (error_rate giảm dần) — 
        dùng cho scale_down filter rule 3.
        True = đang recovering → chặn scale_down
        False = upstream dead hoàn toàn hoặc healthy → cho phép scale_down
        """
        if len(self._history) < 2:
            return False
        for us in self.get_upstream(service):
            if us not in states:
                continue
            current_err = states[us].error_rate
            prev_err = self._history[-2].get(us, {}).get("error_rate", 0)
            # Recovering = error đang giảm nhưng chưa về 0
            if 0 < current_err < prev_err:
                return True
        return False

    # === Obs Vector Features ===

    def to_topology_features(self, service: str, states: dict) -> np.ndarray:
        """
        4D topology features cho per-service obs:
        [0] normalized_tier         (tier / 3.0)
        [1] dependency_health       (avg health ratio of upstream services)
        [2] is_bottleneck           (1.0 nếu service này là bottleneck)
        [3] cascade_risk            (fraction of fleet bị ảnh hưởng nếu die)
        """
        tier_norm = self.get_tier(service) / 3.0

        # Upstream health
        upstreams = self.get_upstream(service)
        if upstreams:
            up_healths = []
            for us in upstreams:
                if us in states:
                    h = states[us].healthy_count / max(states[us].container_count, 1)
                    up_healths.append(h)
            dep_health = sum(up_healths) / len(up_healths) if up_healths else 1.0
        else:
            dep_health = 1.0  # Không có upstream = tự chủ

        is_bn = 1.0 if self._cached_bottleneck == service else 0.0

        cascade = self.get_cascade_impact(service)
        total = len(self._graph)
        cascade_risk = len(cascade) / max(total - 1, 1)

        return np.array([tier_norm, dep_health, is_bn, cascade_risk], dtype=np.float32)

    def print_topology(self):
        """In DAG ra console."""
        print("\n🔗 Service Dependency Graph:")
        print("=" * 50)
        for svc in self.get_processing_order():
            info = self._graph[svc]
            tier_name = SERVICE_TIERS.get(info["tier"], {}).get("name", "?")
            weight = self.get_reward_weight(svc)
            deps = " → ".join(info["depends_on"]) if info["depends_on"] else "(root)"
            impact = self.get_cascade_impact(svc)
            print(f"  [Tier {info['tier']}:{tier_name}] {svc}")
            print(f"    Depends on:     {deps}")
            print(f"    Downstream:     {info['downstream'] or '(leaf)'}")
            print(f"    Cascade impact: {impact or '(none)'}")
            print(f"    Reward weight:  ×{weight}")
        print("=" * 50)
        print(f"  Processing order: {' → '.join(self.get_processing_order())}\n")
