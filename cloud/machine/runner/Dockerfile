FROM rust:1-bookworm AS builder
WORKDIR /usr/src/app
RUN apt-get update && apt-get install -y libssl-dev pkg-config
COPY runner/persistent-vol/ ./persistent-vol/
WORKDIR /usr/src/app/persistent-vol
RUN cargo build --release

WORKDIR /usr/src/app
COPY runner/server/ ./server/
WORKDIR /usr/src/app/server
RUN cargo build --release

FROM node:20-slim AS frontend-builder
WORKDIR /usr/src/app
COPY webapp/package.json webapp/package-lock.json* ./webapp/
WORKDIR /usr/src/app/webapp
RUN npm install --no-audit --no-fund
WORKDIR /usr/src/app
COPY webapp/ ./webapp/
WORKDIR /usr/src/app/webapp
RUN npm run build

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    python3 \
    python3-pip \
    ca-certificates \
    git \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --break-system-packages codepilot-ai==0.9.38
RUN pip install --no-cache-dir --break-system-packages dashscope
RUN pip install --no-cache-dir --break-system-packages voyageai

RUN mkdir -p /opt/codepilot
COPY runner/agent.yaml /opt/codepilot/agent.yaml
# Kept alongside the live file so entrypoint.sh can restore it if a bind
# mount (local self-hosted usage) supplies a missing/empty file on first
# run. On Fly, agent.yaml always already exists from this COPY, so that
# check is always a no-op there.
COPY runner/agent.yaml /opt/codepilot/agent.yaml.default
COPY runner/agent_server.py /opt/codepilot/agent_server.py
COPY runner/web_search_tool.py /opt/codepilot/web_search_tool.py
COPY runner/entrypoint.sh /opt/codepilot/entrypoint.sh
RUN chmod +x /opt/codepilot/entrypoint.sh

COPY --from=builder /usr/src/app/persistent-vol/target/release/snapshot-daemon /usr/local/bin/snapshot-daemon
COPY --from=builder /usr/src/app/server/target/release/codepilot-server /usr/local/bin/codepilot-server
# Baked into the image (not the /workspace persistent volume) — STATIC_PATH
# below points here so a fresh B2-restored /workspace never shadows the UI.
COPY --from=frontend-builder /usr/src/app/runner/frontend/dist /opt/codepilot/frontend/dist

RUN chmod +x /usr/local/bin/snapshot-daemon
RUN chmod +x /usr/local/bin/codepilot-server

ENV STATIC_PATH=/opt/codepilot/frontend/dist
ENV WORKSPACE_PATH=/workspace

WORKDIR /workspace
EXPOSE 8080
CMD ["/opt/codepilot/entrypoint.sh"]
