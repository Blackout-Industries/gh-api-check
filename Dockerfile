# Multi-stage Dockerfile for GitHub Rate Limit Checker
# Best practices: non-root user, minimal image, security scanning compatible

# Stage 1: Builder
FROM python:3.11-slim AS builder

# Set build-time environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --prefix=/install --no-warn-script-location -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

# Metadata labels
LABEL maintainer="DevOps Team" \
      description="GitHub API Rate Limit Checker" \
      version="1.0.0" \
      org.opencontainers.image.source="https://github.com/your-org/github-rate-limit-checker" \
      org.opencontainers.image.licenses="MIT"

# Set runtime environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/home/appuser/.local/bin:$PATH"

# Install runtime dependencies only (security updates)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tini && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Create non-root user with specific UID 1001
RUN groupadd -g 1001 appuser && \
    useradd -r -u 1001 -g appuser -m -s /sbin/nologin \
    -c "Application user" appuser

# Set working directory
WORKDIR /app

# Copy Python dependencies from builder
COPY --from=builder /install /usr/local

# Copy application files
COPY --chown=1001:1001 github_rate_limit_checker.py .

# Create directory for optional private key mounting
RUN mkdir -p /app/secrets && chown -R 1001:1001 /app/secrets

# Switch to non-root user
USER 1001

# Expose metrics port (if using Prometheus mode)
EXPOSE 9090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Use tini as init system (proper signal handling)
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command - can be overridden
CMD ["python", "github_rate_limit_checker.py", "--help"]
