#!/usr/bin/env bash
# scripts/sanity-check.sh — Build and boot a defconfig kernel to verify the install.
#
# This is the final phase of deploy.sh. It proves the entire toolchain works:
# clang, kernel headers, make, QEMU, and the initramfs.
#
# Uses out-of-tree build on /workspace/.kbuild (bind mount).
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

SANITY_DIR=$(mktemp -d)
trap "rm -rf $SANITY_DIR" EXIT

log "Building defconfig ARM64 kernel (out-of-tree on /workspace/.kbuild)..."

# Check if kag-tester image exists (has clang + make + QEMU)
if ! docker image inspect kag-tester >/dev/null 2>&1; then
  die "kag-tester image not built. Run ./bin/deploy.sh first."
fi

# Build the kernel with out-of-tree build.
# On Linux: /workspace/.kbuild (bind mount, native ext4)
# On macOS: /tmp/kbuild (tmpfs, faster than VirtioFS)
docker run --rm \
  -v "${KAG_WORKSPACE}/src:/workspace/src:cached" \
  -v "${KAG_WORKSPACE}/artifacts:/workspace/artifacts" \
  -v "${KAG_VOLUME_CCACHE}:/root/.ccache" \
  -v "${KAG_REPO_ROOT}/subsystems:/opt/subsystems:ro" \
  -e ARCH=arm64 -e LLVM=1 \
  -e CCACHE_DIR=/root/.ccache \
  -e CCACHE_MAXSIZE=20G \
  -e CCACHE_BASEDIR=/ \
  -e CCACHE_NOHASHDIR=true \
  -e CCACHE_COMPILERCHECK=content \
  kag-tester bash -c '
    set -ex
    echo "=== Container started ==="
    echo "Container memory: $(free -m 2>/dev/null || echo unknown)"
    echo "CPU cores: $(nproc)"

    # Create symlink for consistent source path
    rm -f /tmp/src
    ln -s /workspace/src /tmp/src

    # Build dir: /workspace/.kbuild (persistent, no RAM waste)
    mkdir -p /workspace/.kbuild
    mkdir -p /workspace/artifacts/sanity/build
    echo "=== Build dirs ready ==="

    # Clean source tree if it has stale in-tree build artifacts.
    # If the tree is corrupted (symlinks broken by VirtioFS), re-clone.
    if [ -f /workspace/src/scripts/basic/fixdep ] || \
       [ -f /workspace/src/vmlinux ] || \
       [ -f /workspace/src/System.map ] || \
       [ -f /workspace/src/.config ]; then
      echo "=== Cleaning stale build artifacts ==="
      # Try mrproper first
      make -C /workspace/src ARCH=arm64 mrproper 2>&1 || true
      # Then git reset + clean to restore tracked files
      git -C /workspace/src reset --hard HEAD 2>&1 || true
      git -C /workspace/src clean -fdx 2>&1 || true
      # Verify tree is clean
      if [ -f /workspace/src/scripts/basic/fixdep ] || \
         [ -f /workspace/src/.config ]; then
        echo "=== Tree still dirty after clean — re-cloning ==="
        rm -rf /workspace/src
        git clone --depth=1 --branch master \
          https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git \
          /workspace/src 2>&1
      fi
      echo "=== Source tree cleaned ==="
    fi

    echo "=== Running defconfig ==="
    make -C/tmp/src O=/workspace/.kbuild ARCH=arm64 LLVM=1 defconfig 2>&1
    echo "=== Applying mm config fragment ==="
    # Apply the same mm config fragment as the agent builds.
    # This ensures ccache hits across deploy/implementer/tester.
    # Use the kernel merge_config.sh if available, else append.
    MERGE_SCRIPT=/tmp/src/scripts/kconfig/merge_config.sh
    if [ -f "$MERGE_SCRIPT" ]; then
      bash "$MERGE_SCRIPT" -m -O /workspace/.kbuild \
        /workspace/.kbuild/.config /opt/subsystems/mm/config-fragment.config 2>&1
    else
      # Fallback: append fragment to .config
      cat /opt/subsystems/mm/config-fragment.config >> /workspace/.kbuild/.config
    fi
    make -C/tmp/src O=/workspace/.kbuild ARCH=arm64 LLVM=1 olddefconfig 2>&1
    echo "=== Building Image ==="
    make -C/tmp/src O=/workspace/.kbuild ARCH=arm64 LLVM=1 \
         CC="ccache clang" -j$(nproc) Image 2>&1
    echo "=== Image build done ==="

    # Verify Image exists
    if [ ! -f /workspace/.kbuild/arch/arm64/boot/Image ]; then
      echo "ERROR: Image not found at /workspace/.kbuild/arch/arm64/boot/Image"
      echo "Build dir contents:"
      ls -la /workspace/.kbuild/arch/arm64/boot/ 2>/dev/null || true
      exit 1
    fi

    cp /workspace/.kbuild/arch/arm64/boot/Image /workspace/artifacts/sanity/build/Image
    cp /workspace/.kbuild/.config /workspace/artifacts/sanity/build/.config
    cp /workspace/.kbuild/vmlinux /workspace/artifacts/sanity/build/vmlinux 2>/dev/null || true
    cp /workspace/.kbuild/System.map /workspace/artifacts/sanity/build/System.map 2>/dev/null || true
    echo "=== Kernel built successfully ==="

    ccache -s 2>/dev/null | grep -E "Hits:|Misses:|Cache size:" || true
  ' || die "Kernel build failed. See output above."

log "Booting kernel in QEMU for 30 seconds max..."

# Determine QEMU acceleration
QEMU_ACCEL_ARGS="-accel tcg,thread=multi -cpu cortex-a72"
if check_kvm_available; then
  QEMU_ACCEL_ARGS="-enable-kvm -cpu host"
  log "Using KVM acceleration"
else
  log "Using TCG acceleration (cortex-a72)"
fi

# Determine timeout command (macOS does not have timeout by default)
# Use 300s because CONFIG_KUNIT_ALL_TESTS=y runs hundreds of test suites
# at boot, which takes several minutes before init runs.
TIMEOUT_CMD=$(get_timeout_cmd 300)

# Boot the kernel using kag-tester (has QEMU)
$TIMEOUT_CMD docker run --rm \
  -v "${KAG_WORKSPACE}/artifacts:/workspace/artifacts:ro" \
  -v "${KAG_WORKSPACE}/initramfs:/workspace/initramfs:ro" \
  kag-tester qemu-system-aarch64 \
    -machine virt \
    -nodefaults \
    $QEMU_ACCEL_ARGS \
    -smp 2 -m 1G \
    -kernel /workspace/artifacts/sanity/build/Image \
    -initrd /workspace/initramfs/initramfs.cpio.gz \
    -append "console=ttyAMA0 rdinit=/init" \
    -nographic -no-reboot \
    -serial mon:stdio 2>&1 | tee "$SANITY_DIR/boot.log" || true

if grep -q "MM test kernel booted" "$SANITY_DIR/boot.log"; then
  ok "Kernel booted successfully"
  exit 0
else
  die "Kernel did not boot. Boot log: $SANITY_DIR/boot.log"
fi
