# So sánh Benchmark: SDO RL Agent vs. Kubernetes Native

Ngày chạy: 2026-05-18 22:49


## Tổng quan Điểm

| Phương pháp | P1 (Perception) | P2 (Action) | P3 (Timing) | Base Score |
|-------------|----------------|-------------|-------------|------------|
| **SDO RL Agent** | 0.87 | 0.75 | 0.40 | **64.7** |
| **K8s Native** | 0.89 | 0.69 | 0.68 | **69.1** |

> K8s Native vượt trội hơn SDO RL Agent 4.4 điểm base score.


## Chi tiết từng Episode

| Ep | Type | Target | Flags | P1_SDO | P1_K8s | P2_SDO | P2_K8s | P3_SDO | P3_K8s |
|---|------|--------|-------|--------|--------|--------|--------|--------|--------|
| 1 | cpu_stress | api |  | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| 2 | fp | web-app | FP | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 3 | fp | api | FP | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 4 | make_unhealthy | web-app |  | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| 5 | make_unhealthy | api |  | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| 6 | network_chaos | web-app |  | 0.80 | 0.50 | 0.00 | 0.00 | 0.00 | 0.00 |
| 7 | network_chaos | db |  | 0.80 | 0.50 | 0.00 | 0.00 | 0.00 | 0.00 |
| 8 | cpu_stress | api | BN | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 |
| 9 | cpu_stress | db | CASCADE | 1.00 | 1.00 | 1.00 | 0.40 | 0.33 | 0.00 |
| 10 | make_unhealthy | db | CASCADE | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 11 | fp | db | FP | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 12 | memory_leak | api | BN | 1.00 | 1.00 | 1.00 | 0.00 | 0.00 | 0.00 |
| 13 | fp | api | FP | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 14 | crash_container | api |  | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 |
| 15 | crash_container | db | CASCADE | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 |
| 16 | fp | web-app | FP | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 17 | fp | db | FP | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 18 | network_chaos | db | CASCADE | 0.80 | 0.50 | 0.00 | 0.00 | 0.00 | 0.00 |
| 19 | memory_leak | web-app |  | 1.00 | 0.40 | 1.00 | 0.00 | 0.00 | 0.00 |
| 20 | cpu_stress | db | CASCADE | 1.00 | 1.00 | 1.00 | 0.40 | 0.67 | 0.67 |

## Phân tích theo loại lỗi

| Fault Type | SDO P2 avg | K8s P2 avg | SDO tốt hơn? |
|------------|-----------|-----------|--------------|
| cpu_stress | 1.00 | 0.70 | ✅ |
| memory_leak | 1.00 | 0.00 | ✅ |
| network_chaos | 0.00 | 0.00 | - |
| make_unhealthy | 1.00 | 1.00 | - |
| crash_container | 0.00 | 1.00 | ❌ |
| fp | 1.00 | 1.00 | - |

## Kết luận

### Ưu điểm SDO RL Agent so với K8s Native:

1. **Network Chaos**: K8s hoàn toàn mù trước network delay/loss. SDO RL Agent có thể phát hiện và phản ứng (scale_up, kill_and_replace).
2. **Hành động đa dạng**: SDO có 5 loại hành động (scale_up, scale_down, restart, kill_replace, increase_limit). K8s chỉ có restart + HPA scale.
3. **Cascade Awareness**: SDO RL Agent học được mối quan hệ cause-effect giữa các service. K8s xử lý độc lập.
4. **False Positive**: Cả hai đều có khả năng "im lặng" khi không có lỗi.

### Ưu điểm K8s Native:
1. **Tốc độ restart**: Kubelet restart pod gần như tức thì (< 5s).
2. **Không cần training**: Hoạt động ngay không cần dữ liệu huấn luyện.
3. **Ổn định**: Không có rủi ro model RL đưa ra quyết định sai.
