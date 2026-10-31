#!/usr/bin/env bash
# scripts/build-initramfs.sh — Build a minimal initramfs for QEMU boot tests.
set -euo pipefail

KAG_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "${KAG_REPO_ROOT}/bin/_lib.sh"
load_config

# Build initramfs inside a container (busybox-static ships in base image).
# /workspace/initramfs is bind-mounted from host so the result is inspectable.
#
# IMPORTANT: busybox-static is required (not busybox). The dynamically-linked
# busybox won't work in the minimal initramfs because the dynamic linker
# (/lib/ld-linux-aarch64.so.1) and libc are not included. The kernel returns
# ENOENT when it can't find the dynamic linker, causing "No working init found".
docker run --rm \
  -v "${KAG_WORKSPACE}/initramfs:/out" \
  kag-base bash -c '
    set -e
    ROOT=$(mktemp -d)
    mkdir -p $ROOT/{bin,sbin,proc,sys,dev,etc,tmp}

    # Find busybox (could be at /bin/busybox or /usr/bin/busybox depending on package)
    BUSYBOX=""
    for path in /usr/bin/busybox /bin/busybox /usr/bin/busybox-static; do
      if [ -x "$path" ]; then
        BUSYBOX="$path"
        break
      fi
    done
    if [ -z "$BUSYBOX" ]; then
      echo "ERROR: busybox not found in container" >&2
      exit 1
    fi

    # Copy busybox (preserve executable permission)
    cp "$BUSYBOX" $ROOT/bin/busybox
    chmod +x $ROOT/bin/busybox

    # Verify it is statically linked (optional but good to check)
    ldd $ROOT/bin/busybox 2>/dev/null && echo "WARN: busybox is dynamically linked!" || echo "OK: busybox is statically linked"

    # Create symlinks for common commands
    for cmd in sh cat ls echo mount mkdir poweroff grep dmesg free dd; do
      ln -s busybox $ROOT/bin/$cmd
    done

    # Create init script
    cat > $ROOT/init <<INITEOF
#!/bin/busybox sh
/bin/busybox --install -s /bin
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev
echo "=== MM test kernel booted ==="
uname -a
free -m
poweroff -f
INITEOF
    chmod +x $ROOT/init

    # Build initramfs cpio archive
    cd $ROOT && find . | cpio -o -H newc | gzip > /out/initramfs.cpio.gz
    rm -rf $ROOT
  '

ok "initramfs.cpio.gz built at ${KAG_WORKSPACE}/initramfs/initramfs.cpio.gz"
