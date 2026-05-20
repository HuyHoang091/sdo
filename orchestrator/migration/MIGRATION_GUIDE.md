# Hướng dẫn Migrate từ SDO V1 (MultiServiceAgent) lên V2 (Hierarchical RL)

## Tổng quan
Hệ thống Hierarchical Multi-Agent RL mới (V2) thay thế `MultiServiceAgent` cũ bằng bộ đôi `CoreAgent` (điều phối tập trung) và nhiều `ServiceAgent` (chuyên trách cho từng service).

## 1. Yêu cầu hệ thống
- Môi trường và Dependency hoàn toàn tương thích ngược (không yêu cầu cài thêm package).
- Vẫn dùng chung file mô hình ONNX (`sdo_multi_agent.onnx`) hoặc checkpoint (`sdo_agent`).

## 2. Thay đổi File Khởi chạy (Entrypoint)
**Trong `main.py`:**
- **CŨ:** 
  ```python
  from orchestrator.agent import MultiServiceAgent
  agent = MultiServiceAgent(services=SERVICES)
  agent.run()
