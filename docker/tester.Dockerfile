# docker/tester.Dockerfile — tester agent image.
# Built from the 'tester' stage in base.Dockerfile.
# Usage: docker build --target tester -t kag-tester:latest -f base.Dockerfile ..
FROM kag-base:latest
