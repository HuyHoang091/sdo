"""
K8s Chaos Injector — Tương đương orchestrator/chaos.py nhưng dùng kubectl exec.
Inject fault vào Pods trong cluster Kind để benchmark K8s native healing.
"""

import subprocess
import json
import random
import logging
import time
import os

log = logging.getLogger("k8s.chaos")

NAMESPACE = "sdo-benchmark"

# Đảm bảo kubectl nằm trong PATH (cài ở ~/.local/bin)
_bin_dir = os.path.join(os.path.expanduser("~"), ".local", "bin")
if _bin_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _bin_dir + os.pathsep + os.environ.get("PATH", "")

def _kubectl(*args, timeout=30):
    """Chạy kubectl và trả về stdout."""
    cmd = ["kubectl", "-n", NAMESPACE] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1

def get_pods(app_label: str):
    """Lấy danh sách pod name theo label app=<service>."""
    out, rc = _kubectl("get", "pods", "-l", f"app={app_label}",
                       "-o", "jsonpath={.items[*].metadata.name}")
    if rc != 0 or not out:
        return []
    return out.split()

def get_pod_info(app_label: str):
    """Lấy thông tin chi tiết pods dạng JSON."""
    out, rc = _kubectl("get", "pods", "-l", f"app={app_label}", "-o", "json")
    if rc != 0:
        return []
    data = json.loads(out)
    return data.get("items", [])

def _exec_pod(pod_name: str, command: str):
    """kubectl exec vào pod."""
    return _kubectl("exec", pod_name, "--", "sh", "-c", command, timeout=15)

# ============================================================
# 5 LOẠI CHAOS (tương đương ChaosInjector)
# ============================================================

def cpu_stress(service: str, percent: int = 90, duration: int = 30):
    pods = get_pods(service)
    if not pods:
        log.warning("No pods found for %s", service)
        return None
    num_procs = max(4, percent // 10)
    for pod in pods:
        _exec_pod(pod, f"for i in $(seq 1 {num_procs}); do dd if=/dev/zero of=/dev/null bs=1M & done")
    log.info("K8s CPU stress (%d procs × %d pods) on %s", num_procs, len(pods), service)
    return pods[0]

def memory_leak(service: str, mb_per_step: int = 15, duration: int = 30):
    pods = get_pods(service)
    if not pods:
        return None
    total_leak_mb = random.randint(180, 230)  # ~80-90% of 256Mi limit
    steps = 8
    mb_each = max(5, total_leak_mb // steps)
    cmd = f"rm -f /tmp/leak_all; i=0; while [ $i -lt {steps} ]; do head -c {mb_each}m /dev/urandom >> /tmp/leak_all; sleep 3; i=$((i+1)); done"
    for pod in pods:
        _exec_pod(pod, cmd + " &")
    log.info("K8s Memory leak (%dMB in %d steps) on %s", total_leak_mb, steps, service)
    return pods[0]

def network_chaos(service: str, profile: str = "random", duration: int = 30):
    pods = get_pods(service)
    if not pods:
        return None
    delay = random.choice([100, 300, 800, 1500, 3000, 6000])
    jitter = random.randint(0, delay // 3)
    loss = random.choice([0, 0, 5, 10])
    for pod in pods:
        # Install tc if needed
        _exec_pod(pod, "apk add --no-cache iproute2 > /dev/null 2>&1 || true")
        _exec_pod(pod, "tc qdisc del dev eth0 root 2>/dev/null; true")
        _exec_pod(pod, f"tc qdisc add dev eth0 root netem delay {delay}ms {jitter}ms loss {loss}% 2>/dev/null; true")
    log.info("K8s Network chaos on %s: delay=%dms jitter=%dms loss=%d%%", service, delay, jitter, loss)
    return pods[0]

def make_unhealthy(service: str):
    pods = get_pods(service)
    if not pods:
        return None
    pod = random.choice(pods)
    _exec_pod(pod, "nginx -s stop 2>/dev/null; true")
    log.info("K8s Made pod %s unhealthy (stopped nginx)", pod)
    return pod

def crash_container(service: str):
    pods = get_pods(service)
    if not pods:
        return None
    pod = random.choice(pods)
    _exec_pod(pod, "kill 1")
    log.info("K8s Crashed pod %s (killed PID 1)", pod)
    return pod

# ============================================================
# CLEANUP
# ============================================================
def cleanup(service: str):
    pods = get_pods(service)
    for pod in pods:
        _exec_pod(pod, "killall dd stress 2>/dev/null; true")
        _exec_pod(pod, "rm -f /tmp/leak_* /tmp/fill 2>/dev/null; true")
        _exec_pod(pod, "tc qdisc del dev eth0 root 2>/dev/null; true")
        _exec_pod(pod, "nginx 2>/dev/null || true")
    log.info("K8s Chaos cleaned up for %s", service)
