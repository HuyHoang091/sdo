# syntax=docker/dockerfile:1
# ============================================================
# SDO All-In-One — Single Image, Full Stack
#
# Bao gồm: Prometheus + cAdvisor + Node Exporter + SDO Dashboard + SDO UI
# Image tối ưu: gộp layers, static docker binary, xóa cache triệt để
#
# Build (lightweight — chỉ inference):
#   docker build -t sdo:latest .
#
# Build (có khả năng train — thêm PyTorch + SB3):
#   docker build --build-arg INCLUDE_TRAINING=true -t sdo:latest .
#
# Trong compose dùng SDO_CMD để chọn chế độ:
#   SDO_CMD=agent         → chạy agent (mặc định)
#   SDO_CMD=collect       → thu thập chaos data
#   SDO_CMD=train         → train offline
#   SDO_CMD=export-onnx   → export model sang ONNX
# ============================================================

# ============================================================
# 0. Build sdo-ui (multi-stage)
# ============================================================
FROM node:20-alpine AS ui-builder
WORKDIR /app
COPY sdo-ui/package.json sdo-ui/package-lock.json* ./
RUN npm ci
COPY sdo-ui/ ./
RUN npm run build

# ============================================================
# Main image
# ============================================================
FROM python:3.11-slim-bookworm

LABEL maintainer="SDO Project"
LABEL description="Smart Docker Orchestrator — All-in-One (Monitoring + Dashboard + UI)"
LABEL version="2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# === Build ARG ===
ARG INCLUDE_TRAINING=false
ARG PROMETHEUS_VERSION=2.51.1
ARG NODE_EXPORTER_VERSION=1.7.0
ARG CADVISOR_VERSION=0.49.1

WORKDIR /app

# ============================================================
# 1. System deps + Docker CLI + supervisord + nginx
#    Gộp 1 layer, xóa cache triệt để
# ============================================================
RUN apt-get update && \
    apt-get install -y --no-install-recommends --no-install-suggests \
        ca-certificates \
        curl \
        nginx \
        supervisor \
        wget && \
    # Static Docker CLI (không cài engine/daemon)
    curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-26.1.3.tgz" \
        -o /tmp/docker.tgz && \
    tar xzf /tmp/docker.tgz -C /tmp && \
    mv /tmp/docker/docker /usr/local/bin/ && \
    rm -rf /tmp/docker* && \
    apt-get autoremove -y --purge && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*

# ============================================================
# 2. Download monitoring binaries — gộp 1 layer
# ============================================================
RUN wget -q "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION}.linux-amd64.tar.gz" \
        -O /tmp/prometheus.tar.gz && \
    tar xzf /tmp/prometheus.tar.gz -C /tmp/ && \
    mv /tmp/prometheus-${PROMETHEUS_VERSION}.linux-amd64/prometheus /usr/local/bin/ && \
    mv /tmp/prometheus-${PROMETHEUS_VERSION}.linux-amd64/promtool /usr/local/bin/ && \
    \
    wget -q "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz" \
        -O /tmp/node_exporter.tar.gz && \
    tar xzf /tmp/node_exporter.tar.gz -C /tmp/ && \
    mv /tmp/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64/node_exporter /usr/local/bin/ && \
    \
    wget -q "https://github.com/google/cadvisor/releases/download/v${CADVISOR_VERSION}/cadvisor-v${CADVISOR_VERSION}-linux-amd64" \
        -O /usr/local/bin/cadvisor && \
    chmod +x /usr/local/bin/cadvisor && \
    \
    rm -rf /tmp/*

# ============================================================
# 3. Python dependencies — gộp COPY + RUN, xóa pip cache
# ============================================================
COPY requirements-runtime.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt && \
    if [ "$INCLUDE_TRAINING" = "true" ]; then \
        echo "Installing training dependencies (PyTorch + SB3)..." && \
        pip install --no-cache-dir -r requirements.txt; \
    else \
        echo "Skipping training deps (lightweight mode)"; \
    fi && \
    rm -rf /root/.cache/pip /tmp/*

# ============================================================
# 4. Source code + Models
# ============================================================
COPY orchestrator/ ./orchestrator/
COPY grafana/ ./grafana/
COPY models/ ./models/
COPY --from=ui-builder /app/dist /usr/share/nginx/html
RUN mkdir -p logs training_data /etc/prometheus /prometheus

# Nginx config: serve UI + proxy API to dashboard
RUN cat > /etc/nginx/conf.d/default.conf <<'NGINX_CONF'
server {
    listen 80;
    server_name localhost;
    root /usr/share/nginx/html;
    index index.html;

    # Proxy API calls to dashboard backend
    location /api/ {
        proxy_pass http://127.0.0.1:9300;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Proxy WebSocket (training stream)
    location /api/training/stream {
        proxy_pass http://127.0.0.1:9300;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # SPA fallback
    location / {
        try_files $uri $uri/ /index.html;
    }
}
NGINX_CONF

# ============================================================
# 5. Config files (inline — không cần file riêng)
# ============================================================

# Prometheus config
RUN cat > /etc/prometheus/prometheus.yml <<'PROMETHEUS_CONF'
global:
  scrape_interval: 5s
  evaluation_interval: 5s

scrape_configs:
  - job_name: 'cadvisor'
    static_configs:
      - targets: ['localhost:8080']

  - job_name: 'node-exporter'
    static_configs:
      - targets: ['localhost:9100']

  - job_name: 'sdo-agent'
    static_configs:
      - targets: ['localhost:9200']
PROMETHEUS_CONF

# Supervisord config
# BUG FIX: command phải trên 1 dòng (supervisord không hỗ trợ multi-line command)
RUN mkdir -p /etc/supervisor/conf.d && cat > /etc/supervisor/conf.d/sdo.conf <<'SUPERVISOR_CONF'
[supervisord]
nodaemon=true
logfile=/dev/null
logfile_maxbytes=0

[program:prometheus]
command=/bin/sh -c "/usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/prometheus \
  --storage.tsdb.retention.time=7d \
  --web.listen-address=:${SDO_PROM_PORT} \
  --web.enable-lifecycle"
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0
autorestart=true
priority=10

[program:node-exporter]
command=/bin/sh -c "/usr/local/bin/node_exporter \
  --web.listen-address=:${SDO_NODE_EXPORTER_PORT} \
  --path.rootfs=/host"
stdout_logfile=/dev/null
stderr_logfile=/dev/null
autorestart=true
priority=20

[program:cadvisor]
command=/bin/sh -c "/usr/local/bin/cadvisor \
  --port=${SDO_CADVISOR_PORT} \
  --housekeeping_interval=5s \
  --docker_only=true"
stdout_logfile=/dev/null
stderr_logfile=/dev/null
autorestart=true
priority=30

# NOTE: sdo-agent không tự khởi động — phải bật từ UI hoặc supervisorctl
[program:sdo-agent]
command=python -m orchestrator.main agent
directory=/app
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0
autostart=false
autorestart=false
priority=40
startsecs=10

[program:sdo-dashboard]
command=python -m orchestrator.main dashboard
directory=/app
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0
autorestart=true
priority=50
startsecs=5

[program:nginx]
command=/usr/sbin/nginx -g "daemon off;"
stdout_logfile=/dev/null
stderr_logfile=/dev/null
autorestart=true
priority=60
SUPERVISOR_CONF

# ============================================================
# 6. Entrypoint script — xử lý SDO_CMD
# ============================================================
RUN cat > /app/entrypoint.sh <<'ENTRYPOINT_SCRIPT'
#!/bin/bash
set -e

SDO_CMD="${SDO_CMD:-agent}"

cat > /etc/prometheus/prometheus.yml <<EOF
global:
  scrape_interval: 5s
  evaluation_interval: 5s

scrape_configs:
  - job_name: 'cadvisor'
    static_configs:
      - targets: ['localhost:${SDO_CADVISOR_PORT}']

  - job_name: 'node-exporter'
    static_configs:
      - targets: ['localhost:${SDO_NODE_EXPORTER_PORT}']

  - job_name: 'sdo-agent'
    static_configs:
      - targets: ['localhost:${SDO_METRICS_PORT}']
EOF

case "$SDO_CMD" in
  agent)
    echo "=== SDO: Starting All-In-One (Agent + Monitoring) ==="
    exec supervisord -c /etc/supervisor/conf.d/sdo.conf
    ;;
  collect)
    echo "=== SDO: Collecting chaos data ($SDO_COLLECT_EPISODES episodes) ==="
    exec python -m orchestrator.main collect "${SDO_COLLECT_EPISODES:-20}"
    ;;
  train)
    echo "=== SDO: Training offline from $SDO_TRAIN_DATA ==="
    exec python -m orchestrator.main train-offline "${SDO_TRAIN_DATA:-training_data/latest.json}"
    ;;
  train-live)
    echo "=== SDO: Live training ==="
    exec python -m orchestrator.main train-live
    ;;
  export-onnx)
    echo "=== SDO: Exporting SB3 model to ONNX ==="
    exec python -m orchestrator.main export-onnx
    ;;
  status)
    exec python -m orchestrator.main status
    ;;
  monitor)
    exec python -m orchestrator.main monitor
    ;;
  *)
    echo "Unknown SDO_CMD: $SDO_CMD"
    echo "Available: agent, collect, train, train-live, export-onnx, status, monitor"
    exit 1
    ;;
esac
ENTRYPOINT_SCRIPT
RUN chmod +x /app/entrypoint.sh && sed -i 's/\r$//' /app/entrypoint.sh

# ============================================================
# 7. Environment & Ports
# ============================================================
ENV SDO_AGENT_MODE=dry-run
ENV SDO_LOG_LEVEL=INFO
ENV SDO_PROM_PORT=9090        
ENV SDO_METRICS_PORT=9200     
ENV SDO_CADVISOR_PORT=8080    
ENV SDO_NODE_EXPORTER_PORT=9100
ENV SDO_PROMETHEUS_URL=http://localhost:${SDO_PROM_PORT}
ENV SDO_SERVICE=web-app
ENV SDO_CMD=agent
ENV SDO_DASHBOARD_PORT=9300
ENV SDO_WEBHOOK_URLS=http://localhost:${SDO_DASHBOARD_PORT}/api/agent-events

EXPOSE 80 9090 9200 9300

# Healthcheck (dùng wget vì đã cài sẵn)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD sh -c "wget -q --spider http://localhost:${SDO_PROM_PORT}/-/healthy && \
             wget -q --spider http://localhost:${SDO_METRICS_PORT}/metrics || exit 1"

ENTRYPOINT ["/app/entrypoint.sh"]
