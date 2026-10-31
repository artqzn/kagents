# docker/base.Dockerfile — Shared base with all tools.
# Individual agent Dockerfiles copy only what they need from this image.
# This is a build-stage image, not used directly at runtime.
FROM debian:trixie-slim AS base

ENV DEBIAN_FRONTEND=noninteractive

RUN dpkg --add-architecture arm64 2>/dev/null || true && \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential bc bison flex libelf-dev libssl-dev \
    clang-18 lld-18 llvm-18 \
    ccache git python3 python3-pip python3-venv \
    qemu-system-arm qemu-user \
    busybox-static cpio xz-utils \
    sparse \
    sqlite3 \
    openssh-client rsync curl ca-certificates gnupg \
    libsqlite3-dev libssl-dev \
    autoconf automake m4 pkg-config libaio-dev libcap-dev \
    && rm -rf /var/lib/apt/lists/*

# Install cross-compiler only on x86_64 hosts
RUN if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        crossbuild-essential-arm64 && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# Install Docker CLI as static binary (for orchestrator)
RUN DPKG_ARCH=$(dpkg --print-architecture) && \
    case "$DPKG_ARCH" in \
      arm64) DOCKER_ARCH=aarch64 ;; \
      amd64) DOCKER_ARCH=x86_64 ;; \
      *) echo "ERROR: Unsupported arch: $DPKG_ARCH"; exit 1 ;; \
    esac && \
    curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-27.5.1.tgz" \
      -o /tmp/docker.tgz && \
    tar xz -C /tmp -f /tmp/docker.tgz && \
    mv /tmp/docker/docker /usr/local/bin/docker && \
    chmod +x /usr/local/bin/docker && \
    rm -rf /tmp/docker /tmp/docker.tgz && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-${DOCKER_ARCH}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Build smatch (temporarily needs llvm-18-dev)
RUN apt-get update && \
    apt-get install -y --no-install-recommends llvm-18-dev && \
    (git clone --depth=1 https://repo.or.cz/smatch.git /tmp/smatch 2>/dev/null || \
     git clone --depth=1 https://github.com/error27/smatch.git /tmp/smatch 2>/dev/null) && \
    cd /tmp/smatch && \
    make -j$(nproc) PREFIX=/usr/local install && \
    rm -rf /tmp/smatch && \
    apt-get purge -y --auto-remove llvm-18-dev && \
    rm -rf /var/lib/apt/lists/* && \
    echo "smatch built and llvm-18-dev removed" || \
    (apt-get purge -y --auto-remove llvm-18-dev 2>/dev/null; \
     rm -rf /var/lib/apt/lists/*; \
     echo "WARN: smatch build failed — smatch tool will be missing (non-fatal)")

# Build LTP from source
RUN git clone --depth=1 https://github.com/linux-test-project/ltp.git /tmp/ltp && \
    cd /tmp/ltp && \
    make autotools && \
    ./configure --prefix=/opt/ltp --without-tirpc && \
    make -j$(nproc) && \
    make install && \
    rm -rf /tmp/ltp && \
    echo "LTP built and installed to /opt/ltp" || \
    echo "WARN: LTP build failed — LTP tests will be skipped (non-fatal)"

# Python deps
RUN python3 -m pip install --break-system-packages --ignore-installed \
    'litellm>=1.50,<2.0' \
    'langgraph>=0.2,<2.0' \
    'pydantic>=2.0,<3.0' \
    'mcp>=1.0' \
    subprocess-tee \
    PyYAML

# LLVM symlinks
RUN for tool in clang clang++ ld.lld llvm-ar llvm-nm llvm-objcopy llvm-objdump \
                llvm-readelf llvm-strip llvm-symbolizer llvm-readobj \
                llvm-cov llvm-profdata opt llc; do \
      if [ -f "/usr/bin/${tool}-18" ]; then \
        ln -sf "/usr/bin/${tool}-18" "/usr/bin/${tool}"; \
      fi; \
    done

ENV ARCH=arm64 LLVM=1
ENV CC=clang
ENV LLVM_AR=llvm-ar
ENV LLVM_NM=llvm-nm
ENV LLVM_OBJCOPY=llvm-objcopy

ENV GIT_AUTHOR_NAME="Kernel Agents"
ENV GIT_AUTHOR_EMAIL="agents@kernel-agents.local"
ENV GIT_COMMITTER_NAME="Kernel Agents"
ENV GIT_COMMITTER_EMAIL="agents@kernel-agents.local"

WORKDIR /workspace

# ═══════════════════════════════════════════════════════════════════
# Runtime stage: Orchestrator — Python + Docker CLI only (no clang/QEMU)
# ═══════════════════════════════════════════════════════════════════
FROM debian:trixie-slim AS orchestrator
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip sqlite3 git ccache \
    busybox-static cpio \
    && rm -rf /var/lib/apt/lists/*
COPY --from=base /usr/local/bin/docker /usr/local/bin/docker
COPY --from=base /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
RUN python3 -m pip install --break-system-packages --ignore-installed \
    'litellm>=1.50,<2.0' 'langgraph>=0.2,<2.0' 'pydantic>=2.0,<3.0' 'mcp>=1.0' \
    subprocess-tee PyYAML
ENV PYTHONPATH=/opt
ENV GIT_AUTHOR_NAME="Kernel Agents" GIT_AUTHOR_EMAIL="agents@kernel-agents.local"
ENV GIT_COMMITTER_NAME="Kernel Agents" GIT_COMMITTER_EMAIL="agents@kernel-agents.local"
WORKDIR /workspace

# ═══════════════════════════════════════════════════════════════════
# Runtime stage: Planner — Python + git only
# ═══════════════════════════════════════════════════════════════════
FROM debian:trixie-slim AS planner
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git ccache \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --break-system-packages --ignore-installed \
    'litellm>=1.50,<2.0' 'langgraph>=0.2,<2.0' 'pydantic>=2.0,<3.0' 'mcp>=1.0' \
    subprocess-tee PyYAML
ENV PYTHONPATH=/opt
ENV GIT_AUTHOR_NAME="Kernel Agents" GIT_AUTHOR_EMAIL="agents@kernel-agents.local"
ENV GIT_COMMITTER_NAME="Kernel Agents" GIT_COMMITTER_EMAIL="agents@kernel-agents.local"
WORKDIR /workspace

# ═══════════════════════════════════════════════════════════════════
# Runtime stage: Implementer — Python + git + clang + ccache
# ═══════════════════════════════════════════════════════════════════
FROM debian:trixie-slim AS implementer
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git ccache \
    build-essential bc bison flex libelf-dev libssl-dev \
    clang-18 lld-18 llvm-18 \
    && rm -rf /var/lib/apt/lists/* \
    && for tool in clang clang++ ld.lld llvm-ar llvm-nm llvm-objcopy llvm-objdump \
                llvm-readelf llvm-strip llvm-symbolizer llvm-readobj \
                llvm-cov llvm-profdata opt llc; do \
      if [ -f "/usr/bin/${tool}-18" ]; then \
        ln -sf "/usr/bin/${tool}-18" "/usr/bin/${tool}"; \
      fi; \
    done
RUN python3 -m pip install --break-system-packages --ignore-installed \
    'litellm>=1.50,<2.0' 'langgraph>=0.2,<2.0' 'pydantic>=2.0,<3.0' 'mcp>=1.0' \
    subprocess-tee PyYAML
ENV PYTHONPATH=/opt ARCH=arm64 LLVM=1
ENV CC=clang LLVM_AR=llvm-ar LLVM_NM=llvm-nm LLVM_OBJCOPY=llvm-objcopy
ENV GIT_AUTHOR_NAME="Kernel Agents" GIT_AUTHOR_EMAIL="agents@kernel-agents.local"
ENV GIT_COMMITTER_NAME="Kernel Agents" GIT_COMMITTER_EMAIL="agents@kernel-agents.local"
WORKDIR /workspace

# ═══════════════════════════════════════════════════════════════════
# Runtime stage: Reviewer — Python + git + static analyzers
# ═══════════════════════════════════════════════════════════════════
FROM debian:trixie-slim AS reviewer
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git \
    sparse \
    && rm -rf /var/lib/apt/lists/*
COPY --from=base /usr/local/bin/smatch /usr/local/bin/smatch
RUN python3 -m pip install --break-system-packages --ignore-installed \
    'litellm>=1.50,<2.0' 'langgraph>=0.2,<2.0' 'pydantic>=2.0,<3.0' 'mcp>=1.0' \
    subprocess-tee PyYAML
ENV PYTHONPATH=/opt
ENV GIT_AUTHOR_NAME="Kernel Agents" GIT_AUTHOR_EMAIL="agents@kernel-agents.local"
ENV GIT_COMMITTER_NAME="Kernel Agents" GIT_COMMITTER_EMAIL="agents@kernel-agents.local"
WORKDIR /workspace

# ═══════════════════════════════════════════════════════════════════
# Runtime stage: Tester — Python + clang + QEMU + LTP + busybox
# ═══════════════════════════════════════════════════════════════════
FROM debian:trixie-slim AS tester
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git ccache \
    build-essential bc bison flex libelf-dev libssl-dev \
    clang-18 lld-18 llvm-18 \
    qemu-system-arm qemu-user qemu-system-data qemu-efi-aarch64 \
    busybox-static cpio xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && for tool in clang clang++ ld.lld llvm-ar llvm-nm llvm-objcopy llvm-objdump \
                llvm-readelf llvm-strip llvm-symbolizer llvm-readobj \
                llvm-cov llvm-profdata opt llc; do \
      if [ -f "/usr/bin/${tool}-18" ]; then \
        ln -sf "/usr/bin/${tool}-18" "/usr/bin/${tool}"; \
      fi; \
    done
# Copy LTP from base build stage
COPY --from=base /opt/ltp /opt/ltp
RUN python3 -m pip install --break-system-packages --ignore-installed \
    'litellm>=1.50,<2.0' 'langgraph>=0.2,<2.0' 'pydantic>=2.0,<3.0' 'mcp>=1.0' \
    subprocess-tee PyYAML
ENV PYTHONPATH=/opt ARCH=arm64 LLVM=1
ENV CC=clang LLVM_AR=llvm-ar LLVM_NM=llvm-nm LLVM_OBJCOPY=llvm-objcopy
ENV GIT_AUTHOR_NAME="Kernel Agents" GIT_AUTHOR_EMAIL="agents@kernel-agents.local"
ENV GIT_COMMITTER_NAME="Kernel Agents" GIT_COMMITTER_EMAIL="agents@kernel-agents.local"
WORKDIR /workspace

# ═══════════════════════════════════════════════════════════════════
# Runtime stage: Archivist — minimal
# ═══════════════════════════════════════════════════════════════════
FROM debian:trixie-slim AS archivist
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 findutils coreutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
