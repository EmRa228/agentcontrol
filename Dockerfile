FROM python:3.12-slim-bookworm

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        curl \
        ca-certificates \
        git \
        rsync \
        procps \
    && rm -rf /var/lib/apt/lists/*

ARG AGENT_HTTP_PROXY
ARG AGENT_HTTPS_PROXY

RUN set -eux; \
    if [ -n "${AGENT_HTTP_PROXY:-}" ]; then \
      export HTTP_PROXY="${AGENT_HTTP_PROXY}" HTTPS_PROXY="${AGENT_HTTPS_PROXY:-$AGENT_HTTP_PROXY}"; \
      export http_proxy="${AGENT_HTTP_PROXY}" https_proxy="${AGENT_HTTPS_PROXY:-$AGENT_HTTP_PROXY}"; \
    fi; \
    curl -fsSL https://cursor.com/install | bash

WORKDIR /app

COPY requirements.txt .
RUN set -eux; \
    if [ -n "${AGENT_HTTP_PROXY:-}" ]; then \
      export HTTP_PROXY="${AGENT_HTTP_PROXY}" HTTPS_PROXY="${AGENT_HTTPS_PROXY:-$AGENT_HTTP_PROXY}"; \
      export http_proxy="${AGENT_HTTP_PROXY}" https_proxy="${AGENT_HTTPS_PROXY:-$AGENT_HTTP_PROXY}"; \
    fi; \
    pip install --no-cache-dir -r requirements.txt

COPY app.py config.example.yaml xray_client.py ./
COPY templates/ ./templates/
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH="/root/.local/bin:${PATH}" \
    AGENT_BIN=/root/.local/bin/agent

EXPOSE 30228

ENTRYPOINT ["/entrypoint.sh"]
