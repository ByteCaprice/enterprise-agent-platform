# New platform only. No legacy bootstrap, private config, database seed or migration at startup.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS pi
WORKDIR /pi
COPY framework/agent_runtime/pi/node/package*.json ./
RUN npm ci --ignore-scripts --omit=dev

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_PROJECT_ENVIRONMENT=/opt/platform
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY domain/ domain/
COPY infra/ infra/
COPY framework/ framework/
COPY orchestration/ orchestration/
COPY interfaces/ interfaces/
RUN uv sync --locked --no-dev --no-editable
RUN /opt/platform/bin/enterprise-ai check-release

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home platform
COPY --from=builder /opt/platform /opt/platform
COPY --from=pi /usr/local/bin/node /usr/local/bin/node
COPY --from=pi /pi/node_modules /opt/platform/lib/python3.11/site-packages/framework/agent_runtime/pi/node/node_modules
ENV PATH=/opt/platform/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /home/platform
RUN enterprise-ai check-release --pi
EXPOSE 50020
ENTRYPOINT ["enterprise-ai"]
CMD ["serve", "--config-dir", "/etc/enterprise-ai"]
