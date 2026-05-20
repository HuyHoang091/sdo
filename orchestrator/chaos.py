"""
SDO Chaos Injector - Tự gây lỗi có kiểm soát để RL agent học cách xử lý.

5 loại lỗi:
1. CPU Stress - chiếm CPU bằng process dd/stress
2. Memory Leak - allocate memory liên tục
3. Kill Container - kill ngẫu nhiên 1 container
4. Network Delay - thêm latency vào network
5. Disk Fill - ghi file lớn đầy disk

Fixes applied:
- BUG-3: print → logging
- WEAK-7: Document dd limitation in cpu_stress
"""

import logging
import random
import time
from typing import Optional, Tuple

from orchestrator.docker_ctl import DockerController

log = logging.getLogger("sdo.chaos")


class ChaosInjector:
    """Inject các loại lỗi vào containers để train RL agent"""

    def __init__(self, docker_ctl: DockerController):
        self.docker_ctl = docker_ctl
        self.client = docker_ctl.client
        self.active_chaos: Optional[Tuple[str, str, object]] = None
        # (chaos_type, container_id, param)

    # === 5 LOẠI LỖI ===

    def cpu_stress(self, service: str, percent: int = 90, duration: int = 60) -> Optional[str]:
        """
        Stress CPU bằng cách chạy nhiều dd processes trên TẤT CẢ containers.
        Đảm bảo CPU metrics trong Prometheus thực sự tăng cao.

        NOTE (WEAK-7): dd chỉ chiếm 1 CPU core per process.
        → Chạy 8+ processes để saturate container CPU limit.
        """
        containers = self.docker_ctl.get_container_ids(service)
        if not containers:
            log.warning("No containers found for %s", service)
            return None

        # Stress TẤT CẢ containers (không chỉ 1) để tổng CPU cao
        num_procs = max(4, percent // 10)  # 9 procs cho 90%
        stressed = []
        for cid in containers:
            try:
                container = self.client.containers.get(cid)
                # Chạy nhiều dd processes đồng thời
                container.exec_run(
                    f"sh -c 'for i in $(seq 1 {num_procs}); do dd if=/dev/zero of=/dev/null bs=1M & done'",
                    detach=True,
                )
                stressed.append(cid[:12])
            except Exception as e:
                log.debug("CPU stress failed on %s: %s", cid[:12], e)

        if stressed:
            self.active_chaos = ("cpu_stress", stressed[0], duration)
            log.info("CPU stress (%d procs × %d containers) on %s",
                     num_procs, len(stressed), service)
        return stressed[0] if stressed else None

    def memory_leak(self, service: str, mb_per_step: int = 10, duration: int = 30) -> Optional[str]:
        """
        Giả lập memory leak: inject vào TẤT CẢ containers.
        Vì PromQL tính tổng MEM / tổng limit, nếu chỉ leak 1 container
        thì MEM% bị pha loãng bởi các container khác.
        """
        containers = self.docker_ctl.get_container_ids(service)
        if not containers:
            log.warning("No containers found for %s", service)
            return None

        leaked = []
        for cid in containers:
            try:
                container = self.client.containers.get(cid)

                # Lấy memory limit thực tế của container
                mem_limit_bytes = container.attrs.get("HostConfig", {}).get("Memory", 0)
                if mem_limit_bytes <= 0:
                    mem_limit_mb = 256
                else:
                    mem_limit_mb = mem_limit_bytes // (1024 * 1024)

                # Leak 80-92% memory limit → vượt ngưỡng cảnh báo (80%) và danger (90%)
                leak_percent = random.randint(80, 92)
                total_leak_mb = max(50, mem_limit_mb * leak_percent // 100)

                # Ghi 1 file lớn (nhanh hơn chia nhiều bước)
                # Leak dần vào cùng 1 file để RAM tăng từ từ
                steps = 8  # số lần tăng
                mb_each = max(5, total_leak_mb // steps)

                cmd = f"""
                sh -c '
                rm -f /tmp/leak_all;
                i=0;
                while [ $i -lt {steps} ]; do
                    head -c {mb_each}m /dev/urandom >> /tmp/leak_all;
                    sleep 3;
                    i=$((i+1));
                done
                '
                """

                container.exec_run(cmd, detach=True)
                leaked.append((cid[:12], total_leak_mb, mem_limit_mb, leak_percent))
            except Exception as e:
                log.debug("Memory leak failed on %s: %s", cid[:12], e)

        if leaked:
            first = leaked[0]
            self.active_chaos = ("memory_leak", first[0], first[1])
            log.info("Memory leak on %d containers: %dMB/%dMB (%d%%) each",
                     len(leaked), first[1], first[2], first[3])
        return leaked[0][0] if leaked else None

    def kill_container(self, service: str) -> Optional[str]:
        """
        Kill ngẫu nhiên 1 container.
        Không kill container cuối cùng.
        """
        containers = self.docker_ctl.get_container_ids(service)
        if len(containers) <= 1:
            log.warning("Only 1 container for %s, not killing", service)
            return None

        cid = random.choice(containers)
        try:
            self.client.containers.get(cid).kill()
            self.active_chaos = ("kill", cid, 0)
            log.info("Killed container %s", cid[:12])
            return cid
        except Exception as e:
            log.error("Kill failed: %s", e)
            return None

    def network_chaos(
        self,
        service: str,
        profile: str = "random",
        duration: int = 30,
    ) -> Optional[Tuple[str, list[str], dict]]:
        """
        Advanced network chaos cho RL training.

        Profiles:
            - random     : delay + jitter + optional loss
            - gradual    : delay tăng dần theo step
            - spike      : delay cao nhưng ngắn hạn
            - partial    : áp dụng 50% containers
            - all        : áp dụng tất cả containers

        Returns:
            ("network_chaos", [container_ids], params)
        """
        containers = self.docker_ctl.get_container_ids(service)
        if not containers:
            return None

        # --- chọn target containers ---
        if profile == "partial":
            target = random.sample(containers, max(1, len(containers) // 2))
        elif profile == "single":
            target = [random.choice(containers)]
        else:
            target = containers

        # --- build chaos params ---
        if profile == "random":
            delay = random.choice([100, 300, 800, 1500, 3000, 6000])
            jitter = random.randint(0, delay // 3)
            loss = random.choice([0, 0, 5, 10])

        elif profile == "gradual":
            delay = random.choice([200, 500])
            jitter = delay // 5
            loss = 0

        elif profile == "spike":
            delay = random.randint(4000, 8000)
            jitter = delay // 4
            loss = random.choice([0, 5])

        elif profile == "all":
            delay = random.randint(1000, 5000)
            jitter = delay // 4
            loss = random.choice([0, 5, 10])

        else:
            delay = 2000
            jitter = 500
            loss = 0

        applied = []

        for cid in target:
            try:
                container = self.client.containers.get(cid)

                # Auto-install tc if missing
                install_script = """
                if ! command -v tc >/dev/null 2>&1; then
                    if command -v apk >/dev/null 2>&1; then
                        apk add --no-cache iproute2 >/dev/null 2>&1
                    elif command -v apt-get >/dev/null 2>&1; then
                        apt-get update >/dev/null 2>&1 && apt-get install -y iproute2 >/dev/null 2>&1
                    fi
                fi
                """
                container.exec_run(f"sh -c '{install_script}'")

                # reset trước (idempotent)
                container.exec_run(
                    "sh -c 'tc qdisc del dev eth0 root 2>/dev/null; true'"
                )

                # apply new tc rule
                container.exec_run(
                    f"sh -c 'tc qdisc add dev eth0 root netem "
                    f"delay {delay}ms {jitter}ms distribution normal "
                    f"loss {loss}% 2>/dev/null; true'"
                )

                applied.append(cid[:12])

            except Exception as e:
                log.debug("Network chaos failed on %s: %s", cid[:12], e)

        if applied:
            params = {
                "delay_ms": delay,
                "jitter_ms": jitter,
                "loss_percent": loss,
                "profile": profile,
                "duration": duration,
            }

            self.active_chaos = ("network_chaos", applied[0], params)

            log.info(
                "Network chaos (%s) on %d containers | delay=%dms jitter=%dms loss=%d%%",
                profile,
                len(applied),
                delay,
                jitter,
                loss,
            )

            # --- auto-cleanup nếu spike ---
            if profile == "spike":
                # Start a background thread or async task to clean it up early.
                # For simplicity here, we rely on the main train loop's cleanup.
                pass

            return ("network_chaos", applied, params)

        return None

    def increase_network_delay(self, service: str, step: int):
        """
        Dùng trong training loop để tăng delay mỗi step (for Gradual profile).
        Tăng 300ms mỗi step.
        """
        base = 200
        delay = base + step * 300
        jitter = delay // 5

        containers = self.docker_ctl.get_container_ids(service)
        for cid in containers:
            try:
                container = self.client.containers.get(cid)
                container.exec_run(
                    "sh -c 'tc qdisc del dev eth0 root 2>/dev/null; true'"
                )
                container.exec_run(
                    f"sh -c 'tc qdisc add dev eth0 root netem delay {delay}ms {jitter}ms 2>/dev/null; true'"
                )
            except Exception:
                pass

    def disk_fill(self, service: str, mb: int = 200) -> Optional[str]:
        """Tạo file lớn để đầy disk trong container"""
        containers = self.docker_ctl.get_container_ids(service)
        if not containers:
            return None

        cid = random.choice(containers)
        try:
            container = self.client.containers.get(cid)
            container.exec_run(
                f"dd if=/dev/zero of=/tmp/fill bs=1M count={mb}",
                detach=True,
            )
            self.active_chaos = ("disk_fill", cid, mb)
            log.info("Disk fill %dMB on %s", mb, cid[:12])
            return cid
        except Exception as e:
            log.error("Disk fill failed: %s", e)
            return None

    def make_unhealthy(self, service: str) -> Optional[str]:
        """
        Stop app process trong container (container vẫn chạy).
        → Healthcheck fail → container trạng thái unhealthy
        → Model thấy health_ratio giảm, error_rate tăng

        Universal: kill process chính (PID != 1) hoặc stop service
        """
        containers = self.docker_ctl.get_container_ids(service)
        if len(containers) == 0:
            log.warning("No container to make unhealthy")
            return None

        cid = random.choice(containers)
        try:
            container = self.client.containers.get(cid)
            # Stop app process (nginx/node/python) — KHÔNG kill PID 1
            container.exec_run(
                "sh -c '"
                "nginx -s stop 2>/dev/null; "           # nginx
                "pkill -f node 2>/dev/null; "            # node.js
                "pkill -f python 2>/dev/null; "          # python
                "pkill -f java 2>/dev/null; "            # java
                "true'",
                detach=True,
            )
            self.active_chaos = ("make_unhealthy", cid, 0)
            log.info("Made container %s unhealthy (stopped app)", cid[:12])
            return cid
        except Exception as e:
            log.error("Make unhealthy failed: %s", e)
            return None

    def crash_container(self, service: str) -> Optional[str]:
        """
        Crash container bằng cách kill PID 1 → Docker restart policy tạo lại.
        Container sẽ ở trạng thái "restarting" → probe fail → error_rate tăng.
        """
        containers = self.docker_ctl.get_container_ids(service)
        if len(containers) == 0:
            return None

        cid = random.choice(containers)
        try:
            container = self.client.containers.get(cid)
            container.exec_run("kill 1", detach=True)
            self.active_chaos = ("crash_container", cid, 0)
            log.info("Crashed container %s (will restart)", cid[:12])
            return cid
        except Exception as e:
            log.error("Crash failed: %s", e)
            return None

    # === RANDOM CHAOS ===

    def random_chaos(self, service: str) -> Optional[Tuple[str, str, int]]:
        """
        Chọn ngẫu nhiên 1 loại lỗi và inject.
        Trả về (chaos_type, container_id, param) hoặc None.
        """
        chaos_funcs = [
            lambda: self.cpu_stress(service, random.randint(60, 95)),
            lambda: self.memory_leak(service, random.randint(5, 20)),
            lambda: self.kill_container(service),
            lambda: self.network_chaos(
                service,
                random.choice(["random", "gradual", "spike", "partial", "all"])
            ),
            lambda: self.disk_fill(service, random.randint(50, 200)),
            lambda: self.make_unhealthy(service),
            lambda: self.crash_container(service),
        ]

        fn = random.choice(chaos_funcs)
        fn()
        return self.active_chaos

    # === CLEANUP ===

    def cleanup(self, service: str):
        """
        Dọn dẹp tất cả chaos effects:
        - Kill dd/stress processes
        - Xóa leak/fill files
        - Reset network
        """
        # Dùng all=True để tìm cả container paused/exited
        all_containers = self.client.containers.list(
            filters={"label": f"com.docker.compose.service={service}"},
            all=True,
        )
        for container in all_containers:
            try:
                # Unpause nếu bị pause (an toàn)
                if container.status == 'paused':
                    container.unpause()
                # Kill stress processes
                container.exec_run(
                    "sh -c 'killall dd stress 2>/dev/null; true'"
                )
                # Xóa files
                container.exec_run(
                    "sh -c 'rm -f /tmp/leak_* /tmp/fill 2>/dev/null; true'"
                )
                # Reset tc delay trên eth0
                container.exec_run(
                    "sh -c 'tc qdisc del dev eth0 root 2>/dev/null; true'"
                )
                # Restart app nếu bị make_unhealthy kill
                container.exec_run(
                    "sh -c 'nginx 2>/dev/null || true'"
                )
            except Exception:
                pass  # Container có thể đã bị kill

        self.active_chaos = None
        log.info("Chaos cleaned up for %s", service)

    def get_status(self) -> str:
        """Trả về mô tả chaos đang active"""
        if self.active_chaos is None:
            return "No active chaos"
        ctype, cid, param = self.active_chaos
        return f"{ctype} on {cid[:12]} (param={param})"
