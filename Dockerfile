FROM python:3.12-slim AS runtime

ARG VIRTIZAI_VERSION=dev
LABEL org.opencontainers.image.title="VirtizAI" \
      org.opencontainers.image.version="$VIRTIZAI_VERSION" \
      org.opencontainers.image.description="Self-hosted AI orchestration platform"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTIZAI_DATA_DIR=/data \
    VIRTIZAI_WORKSPACE_DIR=/workspace \
    VIRTIZAI_LOG_DIR=/var/log/virtizai \
    VIRTIZAI_APP_VERSION=$VIRTIZAI_VERSION

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin virtizai \
    && mkdir -p /data /workspace /var/log/virtizai \
    && chown -R virtizai:virtizai /data /workspace /var/log/virtizai
WORKDIR /app
COPY pyproject.toml README.md virtizai_cli.py ./
COPY virtizai_core ./virtizai_core
COPY webui ./webui
RUN pip install --no-cache-dir --no-compile . \
    && chown -R virtizai:virtizai /app
USER virtizai
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/healthz', timeout=3)"
ENTRYPOINT ["uvicorn", "virtizai_core.main:app", "--host", "0.0.0.0", "--port", "8766"]
