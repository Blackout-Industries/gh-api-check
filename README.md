# gh-api-check

Check GitHub API rate limits.

## Usage

```bash
export GITHUB_TOKEN=ghp_xxxxx
python github_rate_limit_checker.py
```

### Docker

```bash
docker pull ghcr.io/blackoutindustries/gh-api-check:latest
docker run --rm -e GITHUB_TOKEN=ghp_xxxxx ghcr.io/blackoutindustries/gh-api-check:latest
```

### GitHub App

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_INSTALLATION_ID=987654
export GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem
python github_rate_limit_checker.py
```

### Watch Mode

```bash
python github_rate_limit_checker.py --watch --interval 60
```

### Prometheus Metrics

```bash
python github_rate_limit_checker.py --prometheus-port 9090
```

## Config

Copy `config.example.yaml` to `config.yaml` and fill in credentials.

## Kubernetes

Helm chart in `helm/` directory. Example manifests in `k8s-manifests.yaml`.

## Build

```bash
docker build -t gh-api-check .
```

Automated builds via GitHub Actions on push to main.
