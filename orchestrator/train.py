"""
SDO Training Pipeline - Thu thập data từ chaos + Train RL agent.

3 chế độ:
1. collect: Chạy chaos campaign, ghi data
2. offline: Train PPO từ collected data
3. live: Fine-tune trực tiếp trên Docker

Fixes applied:
- BUG-3: print → logging
- S-4: Dùng MEM_WARNING thay vì hardcode 80
- WEAK-3: Document lambda closure note
"""

import json
import logging
import time
import os
import random
import numpy as np
from datetime import datetime
from typing import Optional

from orchestrator.config import (
    DATA_DIR, MODEL_DIR, MODEL_PATH, LOG_DIR,
    CHAOS_EPISODE_STEPS, CHAOS_STEP_INTERVAL, CHAOS_CLEANUP_WAIT,
    DEFAULT_CHAOS_EPISODES, TRAINING_TIMESTEPS_OFFLINE, TRAINING_TIMESTEPS_LIVE,
    LEARNING_RATE, ACTIONS, get_service_config,
    BOTTLENECK_WINDOW_TICKS,
)

log = logging.getLogger("sdo.train")


class ChaosDataCollector:
    """Thu thập training data từ chaos experiments"""

    def __init__(self, monitor, docker_ctl, chaos, service: str):
        self.monitor = monitor
        self.docker_ctl = docker_ctl
        self.chaos = chaos
        self.service = service
        self.episodes = []
        os.makedirs(DATA_DIR, exist_ok=True)

    def collect_episode(self, chaos_type: Optional[str] = None) -> dict:
        """
        1 episode = inject chaos → quan sát N steps → ghi state+reward
        """
        episode = {
            "chaos_type": chaos_type or "random",
            "timestamp": datetime.now().isoformat(),
            "transitions": [],
        }

        # 1. Ghi state trước chaos
        state_before = self.monitor.get_state(self.service)

        # 2. Inject chaos
        if chaos_type == "network_chaos":
            profile = random.choice(["random", "gradual", "spike", "partial"])
            self.chaos.network_chaos(self.service, profile=profile)

        elif chaos_type and hasattr(self.chaos, chaos_type):
            getattr(self.chaos, chaos_type)(self.service)

        else:
            self.chaos.random_chaos(self.service)

        # 3. Quan sát CHAOS_EPISODE_STEPS steps
        for step in range(CHAOS_EPISODE_STEPS):
            # --- GRADUAL CHAOS ---
            # Nếu chaos là gradual, tăng delay mỗi step
            if self.chaos.active_chaos and self.chaos.active_chaos[0] == "network_chaos":
                chaos_type, _, params = self.chaos.active_chaos
                if isinstance(params, dict) and params.get("profile") == "gradual":
                    self.chaos.increase_network_delay(self.service, step)

            time.sleep(CHAOS_STEP_INTERVAL)

            try:
                state = self.monitor.get_state(self.service)
                vec = self.monitor.to_vector(state)

                # Rule-based labeling: action tốt nhất cho state này
                best_action = self._best_action(state)
                # Step-to-step reward (so với step trước, không phải trước chaos)
                prev = episode["transitions"][-1]["_state_obj"] if episode["transitions"] else state_before
                reward = self._calc_reward(prev, state)

                episode["transitions"].append({
                    "step": step,
                    "state": vec.tolist(),
                    "action": best_action,
                    "reward": round(reward, 4),
                    "raw": {
                        "cpu": round(state.cpu_percent, 2),
                        "mem": round(state.memory_percent, 2),
                        "containers": state.container_count,
                        "healthy": state.healthy_count,
                    },
                    "_state_obj": state,  # Giữ reference cho step-to-step reward
                })

                # Log progress
                action_name = ACTIONS.get(best_action, "?")
                log.info("  Step %d: CPU=%.1f%% MEM=%.1f%% DELAY=%.1fms ERROR_RATE=%.1f%% → best=%s reward=%.2f",
                         step, state.cpu_percent, state.memory_percent, state.response_time_ms, state.error_rate,
                         action_name, reward)

            except Exception as e:
                log.warning("  Step %d: Error: %s", step, e)

        # 4. Cleanup chaos + khôi phục containers
        self.chaos.cleanup(self.service)

        # Khôi phục về số lượng episode bắt đầu (được set bởi run_campaign)
        target = getattr(self, '_episode_start_count', 1)
        current = len(self.docker_ctl.get_container_ids(self.service))
        cfg = get_service_config(self.service)
        # Đảm bảo không dưới min_containers của service
        target = max(target, cfg["min_containers"])
        if current != target:
            log.info("  Restoring containers: %d → %d", current, target)
            self.docker_ctl.scale(self.service, target)

        log.info("  Waiting %ds for stabilization...", CHAOS_CLEANUP_WAIT)
        time.sleep(CHAOS_CLEANUP_WAIT)

        self.episodes.append(episode)
        return episode

    def run_campaign(self, num_episodes: int = DEFAULT_CHAOS_EPISODES) -> str:
        """
        Chạy nhiều episodes chaos, ghi file JSON.
        Trả về đường dẫn file data.
        """
        # Phân bổ chaos types để data đa dạng:
        # cpu_stress nhiều hơn → đảm bảo có đủ scale_up labels
        # network_delay/disk_fill ít → không ảnh hưởng CPU/MEM metrics trên nginx
        chaos_types = [
            "cpu_stress", "cpu_stress", "cpu_stress",  # 30%
            "memory_leak", "memory_leak", "memory_leak",              # 30%
            "make_unhealthy", "make_unhealthy",        # 20%
            "crash_container",                         # 10%
            "network_chaos",                           # 10%
            # "disk_fill",                               # 10%
        ]

        log.info("=" * 60)
        log.info("Starting Chaos Campaign: %d episodes", num_episodes)
        log.info("=" * 60)

        cfg = get_service_config(self.service)
        min_c = cfg["min_containers"]

        # Xoay vòng số container bắt đầu mỗi episode:
        # 30% start với 1, 30% start với 2, 40% start với 3+
        # Để model học cả scale_down và scale_up ở mọi mức
        start_counts = [
            max(min_c, 1), max(min_c, 1), max(min_c, 1),  # 30% - ít container
            max(min_c, 2), max(min_c, 2), max(min_c, 2),  # 30% - vừa
            max(min_c, 3), max(min_c, 3),                  # 20% - nhiều (cho kill_container)
            max(min_c, 4), max(min_c, 4),                  # 20% - dư container (để học scale_down)
        ]

        for i in range(num_episodes):
            chaos = random.choice(chaos_types)

            # Chọn số container bắt đầu cho episode này
            start_count = random.choice(start_counts)
            # kill_container, make_unhealthy, crash_container cần ít nhất 2 containers
            if chaos in ("kill_container", "make_unhealthy", "crash_container") and start_count < 2:
                start_count = 2

            # Scale đến số lượng bắt đầu
            current_count = len(self.docker_ctl.get_container_ids(self.service))
            if current_count != start_count:
                log.info("  Scaling %s: %d → %d containers for episode",
                         self.service, current_count, start_count)
                self.docker_ctl.scale(self.service, start_count)
                time.sleep(10)

            self._episode_start_count = start_count
            log.info("--- Episode %d/%d | Chaos: %s | Start: %d containers ---",
                     i + 1, num_episodes, chaos, start_count)

            try:
                self.collect_episode(chaos_type=chaos)
            except Exception as e:
                log.error("Episode failed: %s", e)
                self.chaos.cleanup(self.service)
                time.sleep(CHAOS_CLEANUP_WAIT)

        # Save — strip _state_obj (không JSON-serializable)
        save_episodes = []
        for ep in self.episodes:
            clean_ep = {k: v for k, v in ep.items()}
            clean_ep["transitions"] = [
                {k: v for k, v in t.items() if k != "_state_obj"}
                for t in ep.get("transitions", [])
            ]
            save_episodes.append(clean_ep)

        filepath = os.path.join(
            DATA_DIR,
            f"chaos_data_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        with open(filepath, "w") as f:
            json.dump(save_episodes, f, indent=2)

        log.info("Collected %d episodes → %s", len(self.episodes), filepath)
        return filepath

    def _best_action(self, state) -> int:
        """Rule-based: chọn action tốt nhất dựa trên PER-SERVICE thresholds (HPA-style)"""
        import math
        cfg = get_service_config(self.service)
        
        # 1. Kubelet Liveness/Readiness Probe behavior (Crash & Fatal errors)
        if state.healthy_count < state.container_count:
            return 3  # restart_unhealthy
        if state.error_rate > 0.5:
            return 6  # kill_and_replace
        elif state.error_rate > 0.3:
            return 3  # restart_unhealthy

        # 2. OOMKiller behavior
        if state.memory_percent > cfg.get("mem_danger", 90.0):
            return 6

        # 3. K8s HPA (Horizontal Pod Autoscaler) Core Algorithm
        target_cpu = cfg.get("cpu_high", 80.0)
        current_cpu = max(0.1, state.cpu_percent)
        current_replicas = state.container_count
        
        ratio = current_cpu / target_cpu
        
        # HPA Native Tolerance (10%)
        if ratio > 1.1 or ratio < 0.9:
            desired_replicas = math.ceil(current_replicas * ratio)
            if desired_replicas > current_replicas:
                return 1  # scale_up
            elif desired_replicas < current_replicas:
                if state.response_time_ms < 500:
                    return 2  # scale_down

        # 4. Custom SDO Edge Cases
        if state.response_time_ms > 1500:
            return 1  # scale_up delay
        if state.disk_io_percent > 85:
            return 6  # kill_and_replace
            
        return 0  # no_op

    def _calc_reward(self, before, after) -> float:
        """Reward cho labeling — so sánh step-to-step"""
        cfg = get_service_config(self.service)
        reward = 0.0
        health = after.healthy_count / max(after.container_count, 1)
        reward += health * 1.0
        cpu_eff = 1.0 - abs(after.cpu_percent - 60) / 60
        reward += max(0, cpu_eff) * 0.5
        if after.memory_percent > cfg["mem_danger"]:
            reward -= 2.0
        elif after.memory_percent > cfg["mem_warning"]:
            reward -= 0.5
        if after.host_cpu_available < 10:
            reward -= 1.0
        # NEW: Penalty cho 3 metrics mới
        # --- NETWORK PENALTY (STRONGER) ---
        if after.response_time_ms > 1500:
            reward -= (after.response_time_ms / 1000) * 0.8

        if after.error_rate > 0.2:
            reward -= after.error_rate * 5.0

        # Nếu delay cao nhưng CPU thấp → tránh scale_down sai
        if after.response_time_ms > 1500 and after.cpu_percent < 50:
            reward -= 1.0

        # Disk penalty
        if after.disk_io_percent > 85:
            reward -= 1.0
        return reward


# === TRAINING FUNCTIONS ===

def train_offline(data_path: str, model_path: Optional[str] = None):
    """
    Train PPO agent từ collected chaos data (offline).
    Không cần Docker đang chạy.
    """
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv
    from orchestrator.rl_env import DockerChaosEnv

    model_path = model_path or MODEL_PATH
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    # Load data
    log.info("Loading data from %s...", data_path)
    with open(data_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    log.info("   Loaded %d episodes", len(data))

    # WEAK-3 NOTE: DummyVecEnv chỉ gọi lambda 1 lần nên closure capture OK.
    # Nếu chuyển sang SubprocVecEnv (multi-process), data cần serialize riêng.
    env = DummyVecEnv([
        lambda: DockerChaosEnv(
            monitor=None, docker_ctl=None, chaos=None,
            service=None, mode="replay", replay_data=data,
        )
    ])

    # Tạo hoặc load model
    if os.path.exists(f"{model_path}.zip"):
        log.info("Loading existing model from %s", model_path)
        model = PPO.load(model_path, env=env)
    else:
        log.info("Creating new PPO model")
        model = PPO(
            "MlpPolicy",
            env,
            learning_rate=LEARNING_RATE,
            n_steps=128,
            batch_size=64,
            n_epochs=10,
            gamma=0.99,
            verbose=1,
            tensorboard_log=LOG_DIR,
        )

    # Train
    log.info("Training for %d timesteps...", TRAINING_TIMESTEPS_OFFLINE)
    model.learn(total_timesteps=TRAINING_TIMESTEPS_OFFLINE)
    model.save(model_path)
    log.info("Model saved to %s", model_path)


def train_live(service: str, model_path: Optional[str] = None):
    """
    Fine-tune PPO agent trực tiếp trên Docker (online learning).
    Cần Docker Compose stack đang chạy.
    """
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv
    from orchestrator.rl_env import DockerChaosEnv
    from orchestrator.monitor import PrometheusMonitor
    from orchestrator.docker_ctl import DockerController
    from orchestrator.chaos import ChaosInjector

    model_path = model_path or MODEL_PATH
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    monitor = PrometheusMonitor()
    docker_ctl = DockerController()
    chaos = ChaosInjector(docker_ctl)

    env = DummyVecEnv([
        lambda: DockerChaosEnv(
            monitor=monitor, docker_ctl=docker_ctl, chaos=chaos,
            service=service, mode="live",
        )
    ])

    if os.path.exists(f"{model_path}.zip"):
        log.info("Loading pretrained model from %s", model_path)
        model = PPO.load(model_path, env=env)
    else:
        log.info("No pretrained model found, starting fresh")
        model = PPO(
            "MlpPolicy", env,
            learning_rate=LEARNING_RATE,
            n_steps=64,
            batch_size=32,
            n_epochs=5,
            gamma=0.99,
            verbose=1,
            tensorboard_log=LOG_DIR,
        )

    log.info("Live training for %d timesteps...", TRAINING_TIMESTEPS_LIVE)
    log.warning("Docker containers will be stressed during training!")
    model.learn(total_timesteps=TRAINING_TIMESTEPS_LIVE)
    model.save(model_path)
    log.info("Model saved to %s", model_path)

# def train_offline_multi(data_path: str, model_path: Optional[str] = None):
#     """
#     Train PPO agent cho multi-service (28D obs) từ collected data.
#     Data phải được thu bởi MultiServiceChaosCollector.
#     """
#     from stable_baselines3 import PPO
#     from stable_baselines3.common.vec_env import DummyVecEnv
#     from orchestrator.rl_env import MultiServiceReplayEnv
#     from orchestrator.topology import ServiceTopology
#     from orchestrator.config import SERVICE_CONFIGS, MULTI_MODEL_PATH

#     model_path = model_path or MULTI_MODEL_PATH
#     os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
#     os.makedirs(LOG_DIR, exist_ok=True)

#     # Load data
#     log.info("Loading multi-service data from %s...", data_path)
#     with open(data_path) as f:
#         data = json.load(f)
#     log.info("   Loaded %d episodes", len(data))

#     # Build topology từ data (lấy services từ episodes)
#     all_services = set()
#     for ep in data:
#         all_services.update(ep.get("services", []))
#     managed = {s: SERVICE_CONFIGS[s] for s in all_services if s in SERVICE_CONFIGS}
#     topology = ServiceTopology(managed)

#     env = DummyVecEnv([
#         lambda: MultiServiceReplayEnv(
#             replay_data=data, topology=topology,
#         )
#     ])

#     # Tạo hoặc load model
#     if os.path.exists(f"{model_path}.zip"):
#         log.info("Loading existing multi-service model from %s", model_path)
#         model = PPO.load(model_path, env=env)
#     else:
#         log.info("Creating new PPO model for multi-service (28D obs)")
#         model = PPO(
#             "MlpPolicy",
#             env,
#             learning_rate=LEARNING_RATE,
#             n_steps=128,
#             batch_size=64,
#             n_epochs=10,
#             gamma=0.99,
#             verbose=1,
#             tensorboard_log=LOG_DIR,
#         )
#         # model = PPO(
#         #     "MlpPolicy",
#         #     env,
#         #     learning_rate=LEARNING_RATE,
#         #     n_steps=512,        # tăng lên để có đủ diverse data mỗi update
#         #     batch_size=128,     # tăng theo
#         #     n_epochs=10,
#         #     gamma=0.99,
#         #     # Thêm entropy để tránh converge sớm về no_op
#         #     ent_coef=0.05,      # ← quan trọng nhất, khuyến khích explore
#         #     verbose=1,
#         #     tensorboard_log=LOG_DIR,
#         # )

#     log.info("Training multi-service model for %d timesteps...", TRAINING_TIMESTEPS_OFFLINE)
#     model.learn(total_timesteps=TRAINING_TIMESTEPS_OFFLINE)
#     model.save(model_path)
#     log.info("Multi-service model saved to %s", model_path)

def train_offline_multi(data_path: str, model_path: Optional[str] = None):
    """Train BC agent cho multi-service (28D obs) từ collected data."""
    from orchestrator.bc_model import BCModel
    from orchestrator.config import MULTI_MODEL_PATH

    model_path = model_path or MULTI_MODEL_PATH
    os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)

    # Load data
    log.info("Loading multi-service data from %s...", data_path)
    with open(data_path) as f:
        data = json.load(f)
    log.info("   Loaded %d episodes", len(data))

    # Train BC model
    model = BCModel()
    model.train_from_data(data, epochs=300, lr=1e-3)

    # Save (dùng .pt thay vì .zip)
    model.save(model_path)
    log.info("Multi-service model saved to %s", model_path)

# =============================================
# MULTI-SERVICE CHAOS COLLECTOR (v2.0 upgrade)
# =============================================

class MultiServiceChaosCollector:
    """
    Thu thập training data cho multi-service scenarios.

    Scenarios:
    1. Cascading: stress tier-1 → observe tier-2/3 degradation
    2. Bottleneck: stress 1 service → label = fix THAT service
    3. Multi-point: stress 2+ services → priority ordering
    4. False positive: CPU cao nhưng downstream OK → NO bottleneck
    """

    def __init__(self, monitor, docker_ctl, chaos, services: list):
        from orchestrator.topology import ServiceTopology
        from orchestrator.reward import UnifiedRewardFunction
        from orchestrator.config import SERVICE_CONFIGS

        self.monitor = monitor
        self.docker_ctl = docker_ctl
        self.chaos = chaos
        self.services = services
        self.episodes = []

        managed = {s: SERVICE_CONFIGS[s] for s in services if s in SERVICE_CONFIGS}
        self.topology = ServiceTopology(managed)
        self.reward_fn = UnifiedRewardFunction(self.topology)
        os.makedirs(DATA_DIR, exist_ok=True)

    def collect_episode(self, target_service: str = None, chaos_type: str = None) -> dict:
        """
        1 episode = inject chaos vào target_service → observe ALL services → label.
        """
        target = target_service or random.choice(self.services)
        episode = {
            "chaos_type": chaos_type or "random",
            "target_service": target,
            "services": self.services,
            "timestamp": datetime.now().isoformat(),
            "transitions": [],
        }

        # Inject chaos vào target
        if chaos_type and hasattr(self.chaos, chaos_type):
            getattr(self.chaos, chaos_type)(target)
        else:
            self.chaos.random_chaos(target)

        # Observe ALL services cho N steps
        for step in range(CHAOS_EPISODE_STEPS):
            time.sleep(CHAOS_STEP_INTERVAL)
            all_states = self.monitor.get_multi_state(self.services)
            self.topology.record_tick(all_states)
            bottleneck = self.topology.detect_bottleneck()

            # if step < BOTTLENECK_WINDOW_TICKS:
            #     log.debug("  Step %d: warm-up (building bottleneck window)", step)
            #     # Vẫn ghi data, bottleneck=None vì chưa đủ window
            #     try:
            #         for svc in self.services:
            #             state = all_states.get(svc)
            #             if not state:
            #                 continue
            #             obs_28d = self.monitor.to_multi_vector(svc, state, all_states, self.topology)
            #             best_action = self._best_action_multi(svc, state, None, all_states)
            #             reward = self.reward_fn.calculate(svc, best_action, state, all_states,
            #                                             self.topology, bottleneck=None)
            #             episode["transitions"].append({
            #                 "step": step,
            #                 "service": svc,
            #                 "tier": self.topology.get_tier(svc),
            #                 "multi_obs": obs_28d.tolist(),
            #                 "state": self.monitor.to_vector(state).tolist(),
            #                 "action": best_action,
            #                 "reward": round(reward, 4),
            #                 "bottleneck": None,
            #                 "raw": {
            #                     "cpu": round(state.cpu_percent, 2),
            #                     "mem": round(state.memory_percent, 2),
            #                     "error_rate": round(state.error_rate, 4),
            #                     "containers": state.container_count,
            #                     "healthy": state.healthy_count,
            #                 },
            #             })
            #             action_name = ACTIONS.get(best_action, "?")
            #             log.info("  Step %d [%s T%d] CPU=%.0f%% MEM=%.1f%% ERR=%.0f%% → %s (r=%.2f) [warm-up]",
            #                      step, svc, self.topology.get_tier(svc),
            #                      state.cpu_percent, state.memory_percent, state.error_rate * 100,
            #                      action_name, reward)
            #     except Exception as e:
            #         log.warning("  Warm-up Step %d error: %s", step, e)
            #     continue

            try:
                # Per-service data
                for svc in self.services:
                    state = all_states.get(svc)
                    if not state:
                        continue

                    obs_28d = self.monitor.to_multi_vector(
                        svc, state, all_states, self.topology
                    )
                    best_action = self._best_action_multi(svc, state, bottleneck, all_states)
                    reward = self.reward_fn.calculate(
                        svc, best_action, state, all_states,
                        self.topology, bottleneck=bottleneck,
                    )

                    episode["transitions"].append({
                        "step": step,
                        "service": svc,
                        "tier": self.topology.get_tier(svc),
                        "multi_obs": obs_28d.tolist(),
                        "state": self.monitor.to_vector(state).tolist(),
                        "action": best_action,
                        "reward": round(reward, 4),
                        "bottleneck": bottleneck,
                        "raw": {
                            "cpu": round(state.cpu_percent, 2),
                            "mem": round(state.memory_percent, 2),
                            "error_rate": round(state.error_rate, 4),
                            "containers": state.container_count,
                            "healthy": state.healthy_count,
                        },
                    })

                    action_name = ACTIONS.get(best_action, "?")
                    bn_tag = " 🚨BN" if svc == bottleneck else ""
                    log.info("  Step %d [%s T%d] CPU=%.0f%% MEM=%.1f%% DL=%.1fms ERR=%.0f%% → %s (r=%.2f)%s",
                             step, svc, self.topology.get_tier(svc),
                             state.cpu_percent, state.memory_percent, state.response_time_ms, state.error_rate * 100,
                             action_name, reward, bn_tag)

            except Exception as e:
                log.warning("  Step %d: Error: %s", step, e)

        # Cleanup
        self.chaos.cleanup(target)
        # Restore all services to min
        for svc in self.services:
            cfg = get_service_config(svc)
            current = len(self.docker_ctl.get_container_ids(svc))
            target_count = max(cfg["min_containers"], 1)
            if current != target_count:
                self.docker_ctl.scale(svc, target_count)

        time.sleep(CHAOS_CLEANUP_WAIT)
        self.episodes.append(episode)
        return episode

    def _best_action_multi(self, service, state, bottleneck, all_states) -> int:
        """
        Topology-aware rule-based labeling.
        Nếu service downstream of bottleneck → no_op.
        Nếu service IS bottleneck → aggressive fix.
        """
        # Downstream of bottleneck → no_op
        if bottleneck and self.topology.is_downstream_of_bottleneck(service):
            return 0

        # Upstream recovering → không scale_down
        cfg = get_service_config(service)
        base_action = self._base_best_action(state, cfg)

        if base_action == 2 and self.topology.is_upstream_recovering(service, all_states):
            return 0

        return base_action

    def _base_best_action(self, state, cfg) -> int:
        """Cùng logic với ChaosDataCollector._best_action (HPA-style)."""
        import math
        
        # 1. Kubelet Liveness/Readiness Probe behavior (Crash & Fatal errors)
        if state.healthy_count < state.container_count:
            return 3  # restart_unhealthy
        if state.error_rate > 0.5:
            return 6  # kill_and_replace
        elif state.error_rate > 0.3:
            return 3  # restart_unhealthy

        # 2. OOMKiller behavior
        if state.memory_percent > cfg.get("mem_danger", 90.0):
            return 6

        # 3. K8s HPA (Horizontal Pod Autoscaler) Core Algorithm
        target_cpu = cfg.get("cpu_high", 80.0)
        current_cpu = max(0.1, state.cpu_percent)
        current_replicas = state.container_count
        
        ratio = current_cpu / target_cpu
        
        # HPA Native Tolerance (10%)
        if ratio > 1.1 or ratio < 0.9:
            desired_replicas = math.ceil(current_replicas * ratio)
            if desired_replicas > current_replicas:
                return 1  # scale_up
            elif desired_replicas < current_replicas:
                if state.response_time_ms < 500:
                    return 2  # scale_down

        # 4. Custom SDO Edge Cases
        if state.response_time_ms > 1500:
            return 1  # scale_up delay
        if state.disk_io_percent > 85:
            return 6  # kill_and_replace
            
        return 0  # no_op

    def run_campaign(self, num_episodes: int = DEFAULT_CHAOS_EPISODES) -> str:
        """Chạy multi-service chaos campaign với đầy đủ scenarios."""

        # === Scenario distribution ===
        # 40% single-service chaos (giống cũ)
        # 20% false positive (CPU cao nhưng downstream OK)
        # 20% multi-point (stress 2+ services cùng lúc)
        # 20% cascading (stress tier-1, observe cascade)
        single_chaos = [
            "cpu_stress", "memory_leak", "make_unhealthy",
            "network_chaos", "crash_container",
        ]

        log.info("=" * 60)
        log.info("Multi-Service Chaos Campaign: %d episodes, %d services",
                 num_episodes, len(self.services))
        log.info("=" * 60)

        for i in range(num_episodes):
            # Chọn scenario type
            r = random.random()
            if r < 0.40:
                # === Single-service chaos ===
                chaos_type = random.choice(single_chaos)
                target = random.choice(self.services)

                if chaos_type in ("kill_container", "make_unhealthy", "crash_container"):
                    cfg = get_service_config(target)
                    if not cfg.get("scalable", True):
                        log.info("  Skipping scale for non-scalable service %s", target)
                        # Đổi sang chaos type khác cho db
                        chaos_type = random.choice(["cpu_stress", "memory_leak", "network_chaos"])
                    else:
                        current = len(self.docker_ctl.get_container_ids(target))
                        if current < 2:
                            self.docker_ctl.scale(target, 2)
                            time.sleep(10)

                log.info("--- Episode %d/%d | SINGLE: %s → %s ---",
                         i + 1, num_episodes, chaos_type, target)
                try:
                    self.collect_episode(target_service=target, chaos_type=chaos_type)
                except Exception as e:
                    log.error("Episode failed: %s", e)
                    self.chaos.cleanup(target)
                    time.sleep(CHAOS_CLEANUP_WAIT)

            elif r < 0.60:
                # === False Positive: CPU cao nhưng downstream OK ===
                # Stress tier-1 nhẹ (batch job simulation), downstream vẫn healthy
                log.info("--- Episode %d/%d | FALSE POSITIVE ---", i + 1, num_episodes)
                try:
                    self._run_false_positive_episode()
                except Exception as e:
                    log.error("False positive episode failed: %s", e)
                    for svc in self.services:
                        self.chaos.cleanup(svc)
                    time.sleep(CHAOS_CLEANUP_WAIT)

            elif r < 0.80:
                # === Multi-point: stress 2+ services ===
                log.info("--- Episode %d/%d | MULTI-POINT ---", i + 1, num_episodes)
                try:
                    self._run_multi_point_episode()
                except Exception as e:
                    log.error("Multi-point episode failed: %s", e)
                    for svc in self.services:
                        self.chaos.cleanup(svc)
                    time.sleep(CHAOS_CLEANUP_WAIT)

            else:
                # === Cascading: stress tier-1, observe cascade ===
                tier1_services = [s for s in self.services if self.topology.get_tier(s) == 1]
                target = random.choice(tier1_services) if tier1_services else random.choice(self.services)
                log.info("--- Episode %d/%d | CASCADE: %s (tier 1) ---",
                         i + 1, num_episodes, target)
                try:
                    self.collect_episode(target_service=target, chaos_type="cpu_stress")
                except Exception as e:
                    log.error("Cascade episode failed: %s", e)
                    self.chaos.cleanup(target)
                    time.sleep(CHAOS_CLEANUP_WAIT)

            if i < num_episodes - 1:
                log.info("⏳ Cooling down %ds before next episode...", CHAOS_CLEANUP_WAIT + 20)
                time.sleep(CHAOS_CLEANUP_WAIT + 20)

        # Save
        filepath = os.path.join(
            DATA_DIR, f"multi_chaos_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        save_eps = []
        for ep in self.episodes:
            clean = {k: v for k, v in ep.items()}
            clean["transitions"] = [
                {k: v for k, v in t.items() if k != "_state_obj"}
                for t in ep.get("transitions", [])
            ]
            save_eps.append(clean)

        with open(filepath, "w") as f:
            json.dump(save_eps, f, indent=2)

        log.info("Multi-service data → %s (%d episodes)", filepath, len(save_eps))
        return filepath

    def _run_false_positive_episode(self):
        """
        False Positive: stress tier-1 nhẹ, downstream vẫn OK.
        Agent phải học: KHÔNG phải bottleneck, KHÔNG block downstream.
        """
        tier1 = [s for s in self.services if self.topology.get_tier(s) == 1]
        target = random.choice(tier1) if tier1 else self.services[0]

        episode = {
            "chaos_type": "false_positive",
            "target_service": target,
            "services": self.services,
            "timestamp": datetime.now().isoformat(),
            "transitions": [],
            "scenario": "false_positive",
        }

        # Stress nhẹ (50% CPU, không đến mức bottleneck threshold 70%)
        self.chaos.cpu_stress(target, duration=CHAOS_EPISODE_STEPS * CHAOS_STEP_INTERVAL)

        for step in range(CHAOS_EPISODE_STEPS):
            time.sleep(CHAOS_STEP_INTERVAL)
            all_states = self.monitor.get_multi_state(self.services)
            self.topology.record_tick(all_states)
            bottleneck = self.topology.detect_bottleneck()

            # if step < BOTTLENECK_WINDOW_TICKS:
            #     log.debug("  Step %d: warm-up", step)
            #     # Vẫn ghi data, bottleneck=None vì chưa đủ window
            #     try:
            #         for svc in self.services:
            #             state = all_states.get(svc)
            #             if not state:
            #                 continue
            #             obs_28d = self.monitor.to_multi_vector(svc, state, all_states, self.topology)
            #             best_action = self._best_action_multi(svc, state, None, all_states)
            #             reward = self.reward_fn.calculate(svc, best_action, state, all_states,
            #                                             self.topology, bottleneck=None)
            #             episode["transitions"].append({
            #                 "step": step,
            #                 "service": svc,
            #                 "tier": self.topology.get_tier(svc),
            #                 "multi_obs": obs_28d.tolist(),
            #                 "state": self.monitor.to_vector(state).tolist(),
            #                 "action": best_action,
            #                 "reward": round(reward, 4),
            #                 "bottleneck": None,
            #                 "raw": {
            #                     "cpu": round(state.cpu_percent, 2),
            #                     "mem": round(state.memory_percent, 2),
            #                     "error_rate": round(state.error_rate, 4),
            #                     "containers": state.container_count,
            #                     "healthy": state.healthy_count,
            #                 },
            #             })
            #             action_name = ACTIONS.get(best_action, "?")
            #             log.info("  Step %d [%s T%d] CPU=%.0f%% MEM=%.1f%% ERR=%.0f%% → %s (r=%.2f) [warm-up]",
            #                      step, svc, self.topology.get_tier(svc),
            #                      state.cpu_percent, state.memory_percent, state.error_rate * 100,
            #                      action_name, reward)
            #     except Exception as e:
            #         log.warning("  Warm-up Step %d error: %s", step, e)
            #     continue

            try:

                for svc in self.services:
                    state = all_states.get(svc)
                    if not state:
                        continue

                    obs_28d = self.monitor.to_multi_vector(svc, state, all_states, self.topology)
                    # Key: downstream KHÔNG bị block → best action = normal rules
                    best_action = self._base_best_action(state, get_service_config(svc))
                    reward = self.reward_fn.calculate(
                        svc, best_action, state, all_states,
                        self.topology, bottleneck=bottleneck,
                    )

                    episode["transitions"].append({
                        "step": step,
                        "service": svc,
                        "tier": self.topology.get_tier(svc),
                        "multi_obs": obs_28d.tolist(),
                        "state": self.monitor.to_vector(state).tolist(),
                        "action": best_action,
                        "reward": round(reward, 4),
                        "bottleneck": bottleneck,  # Should be None most of the time
                        "scenario": "false_positive",
                        "raw": {
                            "cpu": round(state.cpu_percent, 2),
                            "mem": round(state.memory_percent, 2),
                            "error_rate": round(state.error_rate, 4),
                            "containers": state.container_count,
                            "healthy": state.healthy_count,
                        },
                    })
                    action_name = ACTIONS.get(best_action, "?")
                    bn_tag = " 🚨BN" if svc == bottleneck else ""
                    log.info("  Step %d [%s T%d] CPU=%.0f%% MEM=%.1f%% DL=%.1fms ERR=%.0f%% → %s (r=%.2f)%s [FP]",
                             step, svc, self.topology.get_tier(svc),
                             state.cpu_percent, state.memory_percent, state.response_time_ms, state.error_rate * 100,
                             action_name, reward, bn_tag)
            except Exception as e:
                log.warning("  FP Step %d: %s", step, e)

        self.chaos.cleanup(target)
        time.sleep(CHAOS_CLEANUP_WAIT)
        self.episodes.append(episode)

    def _run_multi_point_episode(self):
        """
        Multi-point: stress 2+ services cùng lúc.
        Agent phải ưu tiên tier thấp trước.
        """
        # Chọn 2 services khác tier
        targets = sorted(self.services, key=lambda s: self.topology.get_tier(s))
        if len(targets) >= 2:
            targets = [targets[0], targets[-1]]  # tier-1 + tier-3
        else:
            targets = targets[:1]

        episode = {
            "chaos_type": "multi_point",
            "target_service": ",".join(targets),
            "services": self.services,
            "timestamp": datetime.now().isoformat(),
            "transitions": [],
            "scenario": "multi_point",
        }

        # Stress tất cả targets
        for t in targets:
            cfg = get_service_config(t)
            if cfg.get("scalable", True):
                current = len(self.docker_ctl.get_container_ids(t))
                if current < 2:
                    self.docker_ctl.scale(t, 2)
                    time.sleep(5)
            self.chaos.cpu_stress(t, duration=CHAOS_EPISODE_STEPS * CHAOS_STEP_INTERVAL)

        for step in range(CHAOS_EPISODE_STEPS):
            time.sleep(CHAOS_STEP_INTERVAL)
            all_states = self.monitor.get_multi_state(self.services)
            self.topology.record_tick(all_states)
            bottleneck = self.topology.detect_bottleneck()

            # if step < BOTTLENECK_WINDOW_TICKS:
            #     log.debug("  Step %d: warm-up", step)
            #     # Vẫn ghi data, bottleneck=None vì chưa đủ window
            #     try:
            #         for svc in self.services:
            #             state = all_states.get(svc)
            #             if not state:
            #                 continue
            #             obs_28d = self.monitor.to_multi_vector(svc, state, all_states, self.topology)
            #             best_action = self._best_action_multi(svc, state, None, all_states)
            #             reward = self.reward_fn.calculate(svc, best_action, state, all_states,
            #                                             self.topology, bottleneck=None)
            #             episode["transitions"].append({
            #                 "step": step,
            #                 "service": svc,
            #                 "tier": self.topology.get_tier(svc),
            #                 "multi_obs": obs_28d.tolist(),
            #                 "state": self.monitor.to_vector(state).tolist(),
            #                 "action": best_action,
            #                 "reward": round(reward, 4),
            #                 "bottleneck": None,
            #                 "raw": {
            #                     "cpu": round(state.cpu_percent, 2),
            #                     "mem": round(state.memory_percent, 2),
            #                     "error_rate": round(state.error_rate, 4),
            #                     "containers": state.container_count,
            #                     "healthy": state.healthy_count,
            #                 },
            #             })
            #             action_name = ACTIONS.get(best_action, "?")
            #             log.info("  Step %d [%s T%d] CPU=%.0f%% MEM=%.1f%% ERR=%.0f%% → %s (r=%.2f) [warm-up]",
            #                      step, svc, self.topology.get_tier(svc),
            #                      state.cpu_percent, state.memory_percent, state.error_rate * 100,
            #                      action_name, reward)
            #     except Exception as e:
            #         log.warning("  Warm-up Step %d error: %s", step, e)
            #     continue

            try:

                for svc in self.services:
                    state = all_states.get(svc)
                    if not state:
                        continue

                    obs_28d = self.monitor.to_multi_vector(svc, state, all_states, self.topology)
                    best_action = self._best_action_multi(svc, state, bottleneck, all_states)
                    reward = self.reward_fn.calculate(
                        svc, best_action, state, all_states,
                        self.topology, bottleneck=bottleneck,
                    )

                    episode["transitions"].append({
                        "step": step,
                        "service": svc,
                        "tier": self.topology.get_tier(svc),
                        "multi_obs": obs_28d.tolist(),
                        "state": self.monitor.to_vector(state).tolist(),
                        "action": best_action,
                        "reward": round(reward, 4),
                        "bottleneck": bottleneck,
                        "scenario": "multi_point",
                        "raw": {
                            "cpu": round(state.cpu_percent, 2),
                            "mem": round(state.memory_percent, 2),
                            "error_rate": round(state.error_rate, 4),
                            "containers": state.container_count,
                            "healthy": state.healthy_count,
                        },
                    })

                    action_name = ACTIONS.get(best_action, "?")
                    bn_tag = " 🚨BN" if svc == bottleneck else ""
                    log.info("  Step %d [%s T%d] CPU=%.0f%% MEM=%.1f%% DL=%.1fms ERR=%.0f%% → %s (r=%.2f)%s",
                             step, svc, self.topology.get_tier(svc),
                             state.cpu_percent, state.memory_percent, state.response_time_ms, state.error_rate * 100,
                             action_name, reward, bn_tag)
            except Exception as e:
                log.warning("  MP Step %d: %s", step, e)

        for t in targets:
            self.chaos.cleanup(t)
        time.sleep(CHAOS_CLEANUP_WAIT)
        self.episodes.append(episode)
