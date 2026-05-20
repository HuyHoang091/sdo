"""
SDO Unified Reward Function — SHARED giữa RL env và live agent.
Đảm bảo KHÔNG có training/serving mismatch.
"""

import logging
from typing import Optional
from orchestrator.config import get_service_config, CPU_IDEAL

log = logging.getLogger("sdo.reward")


class UnifiedRewardFunction:
    """
    Tính reward cho 1 action trên 1 service, có fleet context.
    Dùng cho CẢ MultiServiceReplayEnv (training) VÀ MultiServiceAgent (serving).
    """

    def __init__(self, topology=None):
        self.topology = topology

    def calculate(
        self,
        service: str,
        action: int,
        state,
        fleet_states: dict = None,    # None OK khi single-service
        topology=None,
        bottleneck: Optional[str] = None,
    ) -> float:
        """
        Multi-objective reward v3:
        - Tier weight nhân toàn bộ, global floor clip -20.0 cuối hàm
        - Anti-bias penalties hợp nhất bằng max()
        - Bottleneck bonus giảm (3.0/2.0) tránh dominate steady-state
        - No_op downstream chỉ thưởng khi service đang ổn
        - fleet_states optional cho single-service mode
        """
        topo = topology or self.topology
        cfg = get_service_config(service)
        reward = 0.0

        # === 1. Base reward ===
        health = state.healthy_count / max(state.container_count, 1)
        reward += health * 1.0

        cpu_eff = 1.0 - abs(state.cpu_percent - CPU_IDEAL) / CPU_IDEAL
        reward += max(0, cpu_eff) * 0.5

        if state.memory_percent > cfg.get("mem_danger", 90):
            reward -= 2.0
        elif state.memory_percent > cfg.get("mem_warning", 80):
            reward -= 0.5
        else:
            reward += 0.3

        # Host resources — guard cho replay mode
        host_cpu = getattr(state, 'host_cpu_available', 100)
        host_mem = getattr(state, 'host_mem_available', 100)
        if host_cpu < 10:
            reward -= 1.0
        if host_mem < 10:
            reward -= 1.0

        # Network penalties
        # Network delay penalty (luôn áp dụng bất kể action)
        if state.response_time_ms > 1500:
            reward -= (state.response_time_ms / 1000) * 0.8
        if state.error_rate > 0.2:
            reward -= state.error_rate * 5.0
        # Penalty thêm: delay cao nhưng CPU thấp → bottleneck không phải CPU,
        # có thể là downstream issue hoặc network chaos → cần hành động khác scale.
        if state.response_time_ms > 1500 and state.cpu_percent < 50:
            reward -= 1.0
        if state.disk_io_percent > 85:
            reward -= 1.0

        # Waste penalty — dùng min_containers từ config
        min_c = cfg.get("min_containers", 1)
        if state.cpu_percent < cfg.get("cpu_low", 20) and state.container_count > min_c + 1:
            reward -= (state.container_count - min_c - 1) * 0.2

        # Action cost — tách riêng, KHÔNG nhân tier_w.
        # Ý nghĩa: cost hành động là chi phí vận hành thực tế, không phụ thuộc tier.
        _ACTION_COSTS = {0: 0.0, 1: -0.1, 2: -0.05, 3: -0.15, 4: -0.05, 5: -0.05, 6: -0.3}

        action_cost = _ACTION_COSTS.get(action, 0)
        # === 2. Tier weight — nhân toàn bộ base reward (KHÔNG bao gồm action cost) ===
        tier_w = topo.get_reward_weight(service) if topo else 1.0
        reward = reward * tier_w
        reward += action_cost  # Áp dụng flat sau tier scaling

        # === 3. Bottleneck context (giảm bonus tránh dominate steady-state) ===
        # Steady-state reward ~+1.7 (tier-3) ~ +5.1 (tier-1)
        # Bottleneck bonus nên <= 2x steady-state để tránh model chờ bottleneck harvest
        bottleneck_bonus = 0.0
        if bottleneck and topo:
            if service == bottleneck and action in (1, 4, 5):
                bottleneck_bonus = min(3.0 * tier_w, 8.0)    # Giảm từ 5.0→3.0
            elif service == bottleneck and action == 6:
                bottleneck_bonus = min(2.0 * tier_w, 6.0)    # Giảm từ 3.0→2.0
            elif topo.is_downstream_of_bottleneck(service):
                if action == 1:
                    bottleneck_bonus = -min(10.0 * tier_w, 15.0)
                elif action == 0:
                    # Chỉ thưởng no_op nếu service downstream đang ổn
                    # Tránh: agent luôn no_op dù service cần restart/scale
                    if health >= 1.0 and state.error_rate < 0.1:
                        bottleneck_bonus = min(1.0 * tier_w, 3.0)
        reward += bottleneck_bonus

        # === 4. Upstream-aware scale_down ===
        if action == 2 and topo and fleet_states:
            if topo.is_upstream_recovering(service, fleet_states):
                # Flat penalty (không nhân tier_w): mọi service đều bị phạt như nhau
                # khi scale_down lúc upstream recovering, bất kể tier.
                # Lý do: upstream đang hồi phục → traffic sắp trở lại → cần giữ capacity.
                reward -= 5.0

        # === 5. Anti-bias penalties — hợp nhất bằng max(), KHÔNG double-count ===
        # NOTE: Section 1 (state penalty) + Section 5 (action penalty) cộng dồn có chủ đích.
        # Section 1 phạt STATE xấu (bất kể action).
        # Section 5 phạt ACTION sai (scale_down khi system stressed) — additive là đúng.
        anti_bias = 0.0
        if (state.response_time_ms > 1500 or state.error_rate > 0.2) and action == 2:
            anti_bias = max(anti_bias, 10.0)
        if health < 1.0 and action == 2:
            anti_bias = max(anti_bias, 10.0)
        reward -= anti_bias

        # === Global floor — giới hạn penalty tổng cuối cùng ===
        raw_reward = reward
        reward = max(reward, -20.0)

        # Log cảnh báo khi floor trigger (debug training imbalance)
        if raw_reward < -20.0:
            log.debug("Reward floor triggered for %s: raw=%.1f → clipped=-20.0 (action=%d)",
                       service, raw_reward, action)

        return reward