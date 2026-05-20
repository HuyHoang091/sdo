"""
SDO RL Environment - Custom Gymnasium environment cho Docker orchestration.

Hai chế độ:
- replay: Train offline từ collected chaos data
- live: Tương tác trực tiếp với Docker (online learning)

Fixes applied:
- BUG-5: Guard empty replay_data trong _step_replay
- S-3: import time ở đầu file thay vì trong method
"""

import time
import gymnasium as gym
import numpy as np
from typing import Optional
from orchestrator.config import (
    STATE_DIM, NUM_ACTIONS, ACTIONS, MAX_CONTAINERS,
    CPU_IDEAL, get_service_config,
)


class DockerChaosEnv(gym.Env):
    """
    Gym Environment:
    - Observation: 10-dim vector [cpu, mem, net, count, health, host_cpu, host_mem,
                                  response_time, error_rate, disk_io]
    - Actions: 7 discrete [no_op, scale_up, scale_down, restart, +cpu, +mem, kill_replace]
    - Reward: multi-objective (uptime + efficiency - errors - waste)
    """

    metadata = {"render_modes": ["human"]}

    def __init__(
        self,
        monitor,             # PrometheusMonitor (None in replay mode)
        docker_ctl,          # DockerController  (None in replay mode)
        chaos,               # ChaosInjector     (None in replay mode)
        service: str,        # Service name
        mode: str = "live",  # "live" hoặc "replay"
        replay_data=None,    # List of episodes for replay mode
        max_steps: int = 20,
    ):
        super().__init__()

        self.monitor = monitor
        self.docker_ctl = docker_ctl
        self.chaos = chaos
        self.service = service
        self.mode = mode
        self.replay_data = replay_data or []
        self.max_steps = max_steps

        # Spaces
        self.observation_space = gym.spaces.Box(
            low=0.0, high=1.0, shape=(STATE_DIM,), dtype=np.float32
        )
        self.action_space = gym.spaces.Discrete(NUM_ACTIONS)

        # Episode tracking
        self.step_count = 0
        self.episode_idx = 0
        self.current_state = None
        self.prev_state = None

    def reset(self, seed=None):
        super().reset(seed=seed)
        self.step_count = 0

        if self.mode == "live":
            return self._reset_live()
        else:
            return self._reset_replay()

    def step(self, action: int):
        self.step_count += 1

        if self.mode == "live":
            return self._step_live(action)
        else:
            return self._step_replay(action)

    # === LIVE MODE ===

    def _reset_live(self):
        """Reset Docker: scale về min_containers, cleanup chaos, inject mới"""
        # FIX BUG #3: Dùng per-service config thay vì hardcode
        cfg = get_service_config(self.service)
        self.docker_ctl.scale(self.service, cfg["min_containers"])
        self.chaos.cleanup(self.service)
        time.sleep(8)

        # Inject random chaos
        self.chaos.random_chaos(self.service)
        time.sleep(3)

        # Lấy state
        state = self.monitor.get_state(self.service)
        obs = self.monitor.to_vector(state)
        self.current_state = state
        return obs, {}

    def _step_live(self, action: int):
        """Thực thi action trên Docker, đo reward"""
        self.prev_state = self.current_state
        self._execute_action(action)
        time.sleep(5)

        state = self.monitor.get_state(self.service)
        obs = self.monitor.to_vector(state)
        self.current_state = state

        reward = self._calculate_reward(self.prev_state, state, action)
        done = self.step_count >= self.max_steps
        truncated = False

        info = {
            "action_name": ACTIONS[action],
            "cpu": state.cpu_percent,
            "mem": state.memory_percent,
            "containers": state.container_count,
        }

        return obs, reward, done, truncated, info

    def _execute_action(self, action: int):
        """Thực thi action lên Docker"""
        current = self.docker_ctl.count_containers(self.service)

        if action == 0:    # no_op
            pass
        elif action == 1:  # scale_up
            self.docker_ctl.scale_up(self.service)
        elif action == 2:  # scale_down
            if current > 1:
                self.docker_ctl.scale_down(self.service)
        elif action == 3:  # restart_unhealthy
            self.docker_ctl.restart_unhealthy(self.service)
        elif action == 4:  # increase_cpu_limit
            self.docker_ctl.increase_cpu_limit(self.service)
        elif action == 5:  # increase_mem_limit
            self.docker_ctl.increase_mem_limit(self.service)
        elif action == 6:  # kill_and_replace
            self.docker_ctl.kill_and_replace(self.service)

    # === REPLAY MODE ===

    def _reset_replay(self):
        """Load episode tiếp theo từ replay data"""
        if not self.replay_data:
            return np.zeros(STATE_DIM, dtype=np.float32), {}

        if self.episode_idx >= len(self.replay_data):
            self.episode_idx = 0

        ep = self.replay_data[self.episode_idx]
        transitions = ep.get("transitions", [])

        if transitions:
            obs = np.array(transitions[0]["state"], dtype=np.float32)
        else:
            obs = np.zeros(STATE_DIM, dtype=np.float32)

        self.episode_idx += 1
        return obs, {}

    def _step_replay(self, action: int):
        """
        Simulate step từ replay data.
        FIX BUG-5: Guard empty replay_data trước khi modulo.
        """
        # FIX BUG-5: Guard empty data
        if not self.replay_data:
            obs = np.zeros(STATE_DIM, dtype=np.float32)
            return obs, 0.0, True, False, {}

        ep = self.replay_data[(self.episode_idx - 1) % len(self.replay_data)]
        transitions = ep.get("transitions", [])

        if not transitions:
            obs = np.zeros(STATE_DIM, dtype=np.float32)
            return obs, 0.0, True, False, {}

        # FIX BUG #2: step_count đã +1 trước khi vào đây, cần -1
        step_idx = min(self.step_count - 1, len(transitions) - 1)
        if step_idx < 0:
            obs = np.zeros(STATE_DIM, dtype=np.float32)
            return obs, 0.0, True, False, {}

        t = transitions[step_idx]
        obs = np.array(t["state"], dtype=np.float32)

        # Reward: bonus nếu agent chọn đúng best action
        base_reward = t.get("reward", 0.0)
        best_action = t.get("action", 0)

        if action == best_action:
            reward = base_reward + 1.0  # Action chuẩn (tăng weight)
        elif action == 0:
            reward = base_reward - 0.2  # Lười biếng (nhẹ penalty)
        else:
            reward = base_reward - 0.5  # Phạt lệch action so với nhãn

        # 🚨 FIX BIAS: Phạt CỰC NẶNG hành vi "Ngụy Biện" của RL Model
        # Khi delay cao, Traffic nghẽn -> CPU = 1%
        # Model rất hay nhìn nhầm CPU=1% rồi chốt "scale_down" (bất chấp Delay 5000ms).
        # Phải trừ 10 điểm để bẻ cong đường Loss của Deep Learning, bắt nó sợ hãi!
        if obs[7] > 0.5 or obs[8] > 0.2:  # Delay > 1500ms hoặc Error > 20%
            if action == 2:  # Cấm tiệt scale_down
                reward -= 10.0
            elif action != 1 and best_action == 1: # Đáng lẽ phải scale_up mà ko làm
                reward -= 5.0

        # Tương tự với trường hợp Unhealthy (H < 1.0)
        # Bị chết node thì cấm scale_down, phải restart hoặc replace!
        if obs[4] < 1.0:
            if action == 2:
                reward -= 10.0
            elif action != 3 and best_action == 3:
                reward -= 5.0

        done = self.step_count >= self.max_steps or self.step_count >= len(transitions)
        return obs, reward, done, False, {}

    # === REWARD FUNCTION ===

    def _calculate_reward(self, prev_state, curr_state, action: int) -> float:
        """
        Multi-objective reward:

        + Health score:      all healthy = tốt
        + CPU efficiency:    gần 60% = lý tưởng
        + Memory safety:     dưới 80% = an toàn
        - Waste penalty:     quá nhiều container idle
        - Action cost:       mỗi action có chi phí
        + Improvement bonus: state cải thiện so với trước
        """
        reward = 0.0

        if curr_state is None:
            return 0.0

        # 1. Health ratio (0-1)
        if curr_state.container_count > 0:
            health = curr_state.healthy_count / curr_state.container_count
        else:
            health = 0.0
        reward += health * 1.0

        # 2. CPU efficiency: gần CPU_IDEAL = tốt
        cpu_diff = abs(curr_state.cpu_percent - CPU_IDEAL) / CPU_IDEAL
        cpu_score = max(0, 1.0 - cpu_diff)
        reward += cpu_score * 0.5

        # 3. Memory safety — FIX BUG #5: dùng per-service config
        cfg = get_service_config(self.service) if self.service else {}
        mem_danger = cfg.get("mem_danger", 90)
        mem_warning = cfg.get("mem_warning", 80)
        if curr_state.memory_percent > mem_danger:
            reward -= 2.0  # Nguy hiểm
        elif curr_state.memory_percent > mem_warning:
            reward -= 0.5  # Cảnh báo
        else:
            reward += 0.3  # An toàn

        # 4. Host resource headroom
        if curr_state.host_cpu_available < 10:
            reward -= 1.0
        if curr_state.host_mem_available < 10:
            reward -= 1.0

        # 5. Waste penalty: quá nhiều container cho CPU thấp
        cpu_low = cfg.get("cpu_low", 20)
        if curr_state.cpu_percent < cpu_low and curr_state.container_count > 2:
            waste = (curr_state.container_count - 2) * 0.2
            reward -= waste

        # 6. Action cost (tránh action liên tục không cần thiết)
        action_costs = {
            0: 0.0,    # no_op: free
            1: -0.1,   # scale_up: nhẹ
            2: -0.05,  # scale_down: rất nhẹ
            3: -0.15,  # restart: có cost
            4: -0.05,  # cpu limit: nhẹ
            5: -0.05,  # mem limit: nhẹ
            6: -0.3,   # kill_replace: expensive
        }
        reward += action_costs.get(action, 0)

        # 7. Improvement bonus
        if prev_state is not None:
            if prev_state.memory_percent > mem_warning and curr_state.memory_percent < mem_warning:
                reward += 1.0  # Giải quyết memory issue
            cpu_high = cfg.get("cpu_high", 80)
            if prev_state.cpu_percent > cpu_high and curr_state.cpu_percent < cpu_high:
                reward += 0.5  # Giải quyết CPU issue
            # Improvement bonus cho 3 metrics mới
            if prev_state.error_rate > 0.2 and curr_state.error_rate < 0.1:
                reward += 1.0  # Khắc phục error
            if prev_state.response_time_ms > 1500 and curr_state.response_time_ms < 1000:
                reward += 0.5  # Response time cải thiện

        # 8. Penalty cho 3 metrics mới (nhất quán với train.py)
        if curr_state.response_time_ms > 1500:
            reward -= (curr_state.response_time_ms / 1000) * 0.8

        if curr_state.error_rate > 0.2:
            reward -= curr_state.error_rate * 5.0

        if curr_state.response_time_ms > 1500 and curr_state.cpu_percent < 50:
            reward -= 1.0

        if curr_state.disk_io_percent > 85:
            reward -= 1.0  # Disk gần đầy

        return reward

# =============================================
# MULTI-SERVICE REPLAY ENV (v2.0 upgrade)
# =============================================

# class MultiServiceReplayEnv(gym.Env):
#     """
#     Per-service env cho multi-service training (28D obs).
#     CÙNG interface với DockerChaosEnv nhưng obs = 28D (có fleet context).
#     Dùng UnifiedRewardFunction → KHÔNG có training/serving mismatch.

#     Observation: Box(28,)
#     Action: Discrete(7) — cùng 7 actions
#     """

#     metadata = {"render_modes": ["human"]}

#     def __init__(self, replay_data=None, topology=None, max_steps: int = 20):
#         super().__init__()
#         from orchestrator.config import MULTI_STATE_DIM, NUM_ACTIONS
#         from orchestrator.reward import UnifiedRewardFunction

#         self.replay_data = replay_data or []
#         self.topology = topology
#         self.max_steps = max_steps
#         self.reward_fn = UnifiedRewardFunction(topology)

#         self.observation_space = gym.spaces.Box(
#             low=0.0, high=1.0, shape=(MULTI_STATE_DIM,), dtype=np.float32
#         )
#         self.action_space = gym.spaces.Discrete(NUM_ACTIONS)

#         self.step_count = 0
#         self.episode_idx = 0

#     def reset(self, seed=None):
#         super().reset(seed=seed)
#         self.step_count = 0

#         if not self.replay_data:
#             from orchestrator.config import MULTI_STATE_DIM
#             return np.zeros(MULTI_STATE_DIM, dtype=np.float32), {}

#         if self.episode_idx >= len(self.replay_data):
#             self.episode_idx = 0

#         ep = self.replay_data[self.episode_idx]
#         transitions = ep.get("transitions", [])
#         self.episode_idx += 1

#         if transitions and transitions[0].get("multi_obs"):
#             obs = np.array(transitions[0]["multi_obs"], dtype=np.float32)
#         elif transitions:
#             obs = np.array(transitions[0].get("state", [0]*28), dtype=np.float32)
#             # Pad to 28 if needed
#             from orchestrator.config import MULTI_STATE_DIM
#             if len(obs) < MULTI_STATE_DIM:
#                 obs = np.pad(obs, (0, MULTI_STATE_DIM - len(obs)))
#         else:
#             from orchestrator.config import MULTI_STATE_DIM
#             obs = np.zeros(MULTI_STATE_DIM, dtype=np.float32)

#         return obs, {}

#     def step(self, action: int):
#         self.step_count += 1
#         from orchestrator.config import MULTI_STATE_DIM

#         if not self.replay_data:
#             return np.zeros(MULTI_STATE_DIM, dtype=np.float32), 0.0, True, False, {}

#         ep = self.replay_data[(self.episode_idx - 1) % len(self.replay_data)]
#         transitions = ep.get("transitions", [])
#         if not transitions:
#             return np.zeros(MULTI_STATE_DIM, dtype=np.float32), 0.0, True, False, {}

#         step_idx = min(self.step_count - 1, len(transitions) - 1)
#         t = transitions[step_idx]

#         # Build obs (28D if available, else pad)
#         if t.get("multi_obs"):
#             obs = np.array(t["multi_obs"], dtype=np.float32)
#         else:
#             obs = np.array(t.get("state", [0]*28), dtype=np.float32)
#             if len(obs) < MULTI_STATE_DIM:
#                 obs = np.pad(obs, (0, MULTI_STATE_DIM - len(obs)))

#         # Reward: dùng precomputed reward từ MultiServiceChaosCollector
#         # (đã tính bằng UnifiedRewardFunction.calculate() — KHÔNG có mismatch)
#         best_action = t.get("action", 0)
#         base_reward = t.get("reward", 0.0)  # Đã tính đúng bởi collector

#         # Match bonus/penalty trên precomputed reward
#         # if action == best_action and best_action != 0:
#         #     reward = base_reward + 2.0   # tăng từ 1.0 lên 2.0 cho non-no_op đúng
#         # elif action == best_action and best_action == 0:
#         #     reward = base_reward + 0.3   # giảm bonus no_op
#         if action == best_action:
#             reward = base_reward + 1.0   # Chọn đúng
#         elif action == 0:
#             reward = base_reward - 0.2   # Lười biếng
#         else:
#             reward = base_reward - 0.5   # Chọn sai

#         # Anti-bias penalties (không trùng với UnifiedRewardFunction vì đây là
#         # penalty cho hành vi sai của AGENT, không phải state-based reward)
#         # Scale_down khi delay/error cao → CẤM
#         agent_penalty = 0.0
#         if (obs[7] > 0.5 or obs[8] > 0.2) and action == 2:
#             agent_penalty = max(agent_penalty, 8.0)
#         if obs[4] < 1.0 and action == 2:
#             agent_penalty = max(agent_penalty, 8.0)
#         reward -= agent_penalty

#         done = self.step_count >= self.max_steps or self.step_count >= len(transitions)
#         return np.clip(obs, 0, 1).astype(np.float32), reward, done, False, {}
