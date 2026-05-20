# Báo Cáo Đánh Giá Kubernetes Native Healing

Cluster: kind (sdo-bench) — K8s native liveness/readiness probes + HPA

### Điểm tổng hợp (Composite Score): **93.44 / 100**
**Đánh giá:** Acceptable

### Chi tiết từng Episode

|   Ep | Type            | Target   | Flags   |   P1 |   P2 |   P3 |
|-----:|:----------------|:---------|:--------|-----:|-----:|-----:|
|    1 | cpu_stress      | api      |         | 1.00 | 1.00 | 1.00 |
|    2 | fp              | web-app  | FP      | 1.00 | 1.00 | 1.00 |
|    3 | fp              | api      | FP      | 1.00 | 1.00 | 1.00 |
|    4 | make_unhealthy  | web-app  |         | 1.00 | 1.00 | 1.00 |
|    5 | make_unhealthy  | api      |         | 1.00 | 1.00 | 1.00 |
|    6 | network_chaos   | web-app  |         | 0.50 | 0.00 | 0.00 |
|    7 | network_chaos   | db       |         | 0.50 | 0.00 | 0.00 |
|    8 | cpu_stress      | api      | BN      | 1.00 | 1.00 | 1.00 |
|    9 | cpu_stress      | db       | CASCADE | 1.00 | 0.40 | 0.00 |
|   10 | make_unhealthy  | db       | CASCADE | 1.00 | 1.00 | 1.00 |
|   11 | fp              | db       | FP      | 1.00 | 1.00 | 1.00 |
|   12 | memory_leak     | api      | BN      | 1.00 | 0.00 | 0.00 |
|   13 | fp              | api      | FP      | 1.00 | 1.00 | 1.00 |
|   14 | crash_container | api      |         | 1.00 | 1.00 | 1.00 |
|   15 | crash_container | db       | CASCADE | 1.00 | 1.00 | 1.00 |
|   16 | fp              | web-app  | FP      | 1.00 | 1.00 | 1.00 |
|   17 | fp              | db       | FP      | 1.00 | 1.00 | 1.00 |
|   18 | network_chaos   | db       | CASCADE | 0.50 | 0.00 | 0.00 |
|   19 | memory_leak     | web-app  |         | 0.40 | 0.00 | 0.00 |
|   20 | cpu_stress      | db       | CASCADE | 1.00 | 0.40 | 0.67 |

### Chỉ số Bonus/Penalty
- Cascade Root Cause Bonus: +20
- Bottleneck Detection Bonus: +6
- False Positive Penalty: -1.7 (Rate: 17%)
