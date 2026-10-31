# docker/orchestrator.Dockerfile — Orchestrator image.
# Built from the 'orchestrator' stage in base.Dockerfile.
# Usage: docker build --target orchestrator -t kag-base:latest -f base.Dockerfile ..
# Note: orchestrator uses kag-base tag for backward compatibility with compose.yml
FROM kag-base:latest
