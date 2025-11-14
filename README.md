# gh-api-check | [![Build and Release](https://github.com/Blackout-Industries/gh-api-check/actions/workflows/build-release.yml/badge.svg)](https://github.com/Blackout-Industries/gh-api-check/actions/workflows/build-release.yml)

Monitor GitHub API rate limits for single or multiple GitHub Apps from a single deployment.

## Features

- ✅ Monitor single or multiple GitHub Apps simultaneously
- ✅ Prometheus metrics export with app-level labels
- ✅ Support for Personal Access Tokens and GitHub App authentication
- ✅ Helm chart with External Secrets Operator integration
- ✅ Grafana dashboard with label-based filtering
- ✅ Concurrent rate limit checking for multiple apps
- ✅ Token caching and automatic refresh

## Usage

### Single App (Backward Compatible)

```bash
export GITHUB_TOKEN=ghp_xxxxx
python github_rate_limit_checker.py --prometheus-port 9090
```

### Multiple Apps from Config File

```bash
python github_rate_limit_checker.py --config-file /app/config/apps.json --prometheus-port 9090
```

Example `apps.json`:
```json
{
  "apps": [
    {
      "name": "devops-runner",
      "app_id": "1187645",
      "installation_id": "63220290",
      "private_key_path": "/app/secrets/app1.pem"
    },
    {
      "name": "ci-automation",
      "app_id": "2345678",
      "installation_id": "87654321",
      "private_key_path": "/app/secrets/app2.pem"
    }
  ]
}
```

### Docker

```bash
docker pull ghcr.io/blackoutindustries/gh-api-check:latest

# Single app
docker run --rm -e GITHUB_TOKEN=ghp_xxxxx ghcr.io/blackoutindustries/gh-api-check:latest

# Multiple apps with config
docker run --rm \
  -v $(pwd)/apps.json:/app/config/apps.json \
  -v $(pwd)/secrets:/app/secrets \
  ghcr.io/blackoutindustries/gh-api-check:latest \
  --config-file /app/config/apps.json --prometheus-port 9090
```

### GitHub App Authentication

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_INSTALLATION_ID=987654
export GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem
python github_rate_limit_checker.py --prometheus-port 9090
```

### Watch Mode

```bash
python github_rate_limit_checker.py --watch --interval 60
```

## Kubernetes Deployment

### Single App (Default)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: gh-api-check
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/Blackout-Industries/gh-api-check.git
    targetRevision: main
    path: helm/github-rate-limit-checker
    helm:
      releaseName: gh-api-check
      values: |
        github:
          authMethod: "app"
          app:
            appId: "1187645"
            installationId: "63220290"
            privateKey:
              externalSecret:
                enabled: true
                secretStoreName: oci-auth-cluster
                secretStoreKind: ClusterSecretStore
                remoteSecrets:
                  oracle:
                    secretName: ghe_devops_runner_app_pem
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
```

### Multiple Apps

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: gh-api-check-multi
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/Blackout-Industries/gh-api-check.git
    targetRevision: main
    path: helm/github-rate-limit-checker
    helm:
      releaseName: gh-api-check-multi
      valueFiles:
        - values-multiapp-example.yaml
      values: |
        multiApp:
          enabled: true
          apps:
            - name: "devops-runner"
              appId: "1111111111"
              installationId: "2222222222"
              privateKey:
                externalSecret:
                  enabled: true
                  secretStoreName: oci-auth-cluster
                  secretStoreKind: ClusterSecretStore
                  remoteSecrets:
                    oracle:
                      secretName: ghe_ci_automation_app_pem1
                    aws: null
                    azure: null
                    gcp: null
                    vault: null
            - name: "ci-automation"
              appId: "2345678"
              installationId: "87654321"
              privateKey:
                externalSecret:
                  enabled: true
                  secretStoreName: oci-auth-cluster
                  secretStoreKind: ClusterSecretStore
                  remoteSecrets:
                    oracle:
                      secretName: ghe_ci_automation_app_pem2
                    aws: null
                    azure: null
                    gcp: null
                    vault: null
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
```

See `helm/github-rate-limit-checker/values-multiapp-example.yaml` for a complete example.

## Prometheus Metrics

Metrics are exported with labels for filtering:

```promql
# Check app status (1=healthy, 0=error)
github_app_status{app_name="devops-runner"}

# Core API remaining calls
github_rate_limit_remaining{resource="core",app_name="devops-runner"}

# Filter by app_id
github_rate_limit_remaining{app_id="1187645"}

# All apps, specific resource
github_rate_limit_remaining{resource="actions_runner_registration"}
```

### Available Metrics

- `github_app_status` - App health status (1=healthy, 0=error)
- `github_rate_limit_limit` - Rate limit maximum
- `github_rate_limit_remaining` - Remaining API calls
- `github_rate_limit_used` - Used API calls
- `github_rate_limit_reset` - Unix timestamp when limit resets
- `github_graphql_rate_limit_limit` - GraphQL rate limit maximum
- `github_graphql_rate_limit_remaining` - GraphQL remaining calls
- `github_graphql_rate_limit_used` - GraphQL used calls

### Labels

- `app_name` - Name of the GitHub App (e.g., "devops-runner")
- `app_id` - GitHub App ID (e.g., "1187645")
- `installation_id` - Installation ID (e.g., "63220290")
- `resource` - API resource type (core, search, graphql, actions_runner_registration, etc.)

## Grafana Dashboard

Import the dashboard from `grafana/dashboards/github-rate-limits.json`.

Features:
- App status overview
- Per-app rate limit trends
- Resource-specific gauges
- Label-based filtering (filter by app_name)
- Multi-app comparison

## Build

```bash
docker build -t gh-api-check .
```

Automated builds via GitHub Actions on push to main.

## Architecture

### Single App Mode (Default)
- Backward compatible with existing deployments
- Uses environment variables or single ExternalSecret
- Metrics include `app_name="default"` label

### Multi-App Mode
- Monitors 1-N GitHub Apps from single pod
- ConfigMap for app list configuration
- One ExternalSecret per app for credentials
- Concurrent rate limit checking with ThreadPoolExecutor
- Token caching (1-hour expiry with 5-min refresh buffer)
- Metrics include unique `app_name`, `app_id`, and `installation_id` labels

## Requirements

- Python 3.11+
- PyJWT (for GitHub App authentication)
- cryptography
- requests

For Kubernetes:
- External Secrets Operator (for secret management)
- Prometheus (for metrics scraping)
- Grafana (for dashboards)
