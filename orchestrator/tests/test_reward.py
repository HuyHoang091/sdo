"""
Sanity check cho UnifiedRewardFunction.
Chạy: python -m pytest tests/test_reward.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace
from orchestrator.reward import UnifiedRewardFunction


def make_state(cpu=50, mem=50, healthy=2, total=2, delay=100, error=0.0,
               disk_io=10, host_cpu=80, host_mem=80, net_rx=100, restart=0):
    return SimpleNamespace(
        cpu_percent=cpu, memory_percent=mem,
        healthy_count=healthy, container_count=total,
        response_time_ms=delay, error_rate=error,
        disk_io_percent=disk_io, host_cpu_available=host_cpu,
        host_mem_available=host_mem, network_rx_rate=net_rx,
        restart_count=restart, memory_bytes=0,
    )


class FakeTopology:
    def get_tier(self, svc):
        return {"db": 1, "api": 2, "web-app": 3}.get(svc, 3)

    def get_reward_weight(self, svc):
        return {1: 3.0, 2: 2.0, 3: 1.0}[self.get_tier(svc)]

    def is_downstream_of_bottleneck(self, svc):
        return False

    def is_upstream_recovering(self, svc, fleet):
        return False


topo = FakeTopology()
reward_fn = UnifiedRewardFunction(topo)


def test_healthy_noop_returns_positive():
    state = make_state(cpu=50, mem=60, healthy=2, total=2, delay=100, error=0)
    r = reward_fn.calculate("web-app", action=0, state=state, fleet_states={})
    assert r > 0, f"Expected positive for healthy no-op, got {r}"


def test_tier1_healthy_noop_higher_than_tier3():
    state = make_state(cpu=50, mem=60, healthy=2, total=2, delay=100, error=0)
    r1 = reward_fn.calculate("db", action=0, state=state, fleet_states={})
    r3 = reward_fn.calculate("web-app", action=0, state=state, fleet_states={})
    assert r1 > r3, f"Tier-1 reward ({r1}) should > tier-3 ({r3})"


def test_scaledown_when_delay_high_heavily_penalized():
    state = make_state(cpu=10, mem=50, healthy=2, total=2, delay=3000, error=0)
    r = reward_fn.calculate("web-app", action=2, state=state, fleet_states={})
    assert r < -10.0, f"Expected < -10, got {r}"


def test_scaledown_when_unhealthy_heavily_penalized():
    state = make_state(cpu=30, mem=50, healthy=1, total=2, delay=100, error=0)
    r = reward_fn.calculate("api", action=2, state=state, fleet_states={})
    assert r < -5.0, f"Expected < -5 for scale_down when unhealthy, got {r}"


def test_floor_not_triggered_in_normal_conditions():
    state = make_state(cpu=50, mem=60, healthy=2, total=2, delay=100, error=0)
    r = reward_fn.calculate("web-app", action=0, state=state, fleet_states={})
    assert r > -20.0, f"Floor should not trigger in normal, got {r}"


def test_floor_caps_extreme_penalty():
    """Tier-1 + delay cao + error cao + scale_down → phải hit floor"""
    state = make_state(cpu=10, mem=95, healthy=0, total=3, delay=5000, error=0.8, disk_io=90)
    r = reward_fn.calculate("db", action=2, state=state, fleet_states={})
    assert r == -20.0, f"Expected floor -20.0, got {r}"


def test_kill_replace_cost_is_flat():
    """Action cost không nhân tier_w"""
    s = make_state(cpu=50, mem=60, healthy=2, total=2, delay=100, error=0)
    r_noop = reward_fn.calculate("db", action=0, state=s, fleet_states={})
    r_kill = reward_fn.calculate("db", action=6, state=s, fleet_states={})
    diff = r_noop - r_kill
    assert 0.25 < diff < 0.35, f"Kill cost diff should be ~0.3 (flat), got {diff}"


if __name__ == "__main__":
    tests = [f for f in dir() if f.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            globals()[t]()
            print(f"  ✅ {t}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {t}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
