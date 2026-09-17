FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install --no-warn-script-location -r requirements.txt


FROM python:3.11-slim

ARG VERSION=dev

LABEL maintainer="DevOps Team" \
      description="GitHub API Rate Limit Checker" \
      version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GH_API_CHECK_VERSION="${VERSION}" \
    PATH="/home/appuser/.local/bin:$PATH"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

RUN groupadd -g 1001 appuser && \
    useradd -r -u 1001 -g appuser -m -s /sbin/nologin \
    -c "Application user" appuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY --chown=1001:1001 github_rate_limit_checker.py .

RUN mkdir -p /app/secrets && chown -R 1001:1001 /app/secrets

USER 1001

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9090/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["python", "github_rate_limit_checker.py", "--help"]
