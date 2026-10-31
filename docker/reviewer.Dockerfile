# docker/reviewer.Dockerfile — reviewer agent image.
# Built from the 'reviewer' stage in base.Dockerfile.
# Usage: docker build --target reviewer -t kag-reviewer:latest -f base.Dockerfile ..
FROM kag-base:latest
