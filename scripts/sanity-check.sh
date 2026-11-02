#!/usr/bin/env bash
# scripts/sanity-check.sh — Build and boot a defconfig kernel to verify the install.
#
# This is the final phase of deploy.sh. It proves the entire toolchain works:
# clang, kernel headers, make, QEMU, and the initramfs.
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

SANITY_DIR=$(mktemp -d)
trap "rm -rf $SANITY_DIR" EXIT

log "Building defconfig ARM64 kernel in a container..."

# Check if kag-tester image exists (has clang + make + QEMU)
if ! docker image inspect kag-tester >/dev/null 2>&1; then
  die "kag-tester image not built. Run ./bin/deploy.sh first."
fi

# Build the kernel using kag-tester (has clang + make + ccache)
docker run --rm \
  -v "${KAG_WORKSPACE}/src:/workspace/src:cached" \
  -v "${KAG_VOLUME_BUILD}:/workspace/build" \
  -v "${KAG_VOLUME_CCACHE}:/root/.ccache" \
  -e ARCH=arm64 -e LLVM=1 \
  kag-tester bash -c '
    set -e
    cd /workspace/src
    make defconfig
    make -j$(nproc) CC="ccache clang" Image
    cp arch/arm64/boot/Image /workspace/build/Image.sanity
    echo "Kernel built successfully"
  '

log "Booting kernel in QEMU for 30 seconds max..."

# Determine QEMU acceleration
QEMU_ACCEL_ARGS="-accel tcg,thread=multi -cpu cortex-a72"
if check_kvm_available; then
  QEMU_ACCEL_ARGS="-enable-kvm -cpu host"
  log "Using KVM acceleration"
else
  log "Using TCG acceleration (cortex-a72)"
fi

# Determine timeout command (macOS doesn't have `timeout` by default)
TIMEOUT_CMD=$(get_timeout_cmd 30)

# Boot the kernel using kag-tester (has QEMU)
$TIMEOUT_CMD docker run --rm \
  -v "${KAG_VOLUME_BUILD}:/workspace/build" \
  -v "${KAG_WORKSPACE}/initramfs:/workspace/initramfs:ro" \
  kag-tester qemu-system-aarch64 \
    -machine virt \
    -nodefaults \
    $QEMU_ACCEL_ARGS \
    -smp 2 -m 1G \
    -kernel /workspace/build/Image.sanity \
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
