"""Framework configuration — reads from environment variables."""
import os
from pathlib import Path


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ─── Paths ──────────────────────────────────────────────────────────
WORKSPACE = Path(_get_env("KAG_WORKSPACE", "/workspace"))
FRAMEWORK = Path(_get_env("KAG_FRAMEWORK_DIR", "/opt/framework"))
SUBSYSTEMS = Path(_get_env("KAG_SUBSYSTEMS_DIR", "/opt/subsystems"))

# ─── Active subsystem ───────────────────────────────────────────────
SUBSYSTEM_NAME = _get_env("KAG_SUBSYSTEM", "mm")

# ─── Timeouts ───────────────────────────────────────────────────────
AGENT_TIMEOUT = int(_get_env("KAG_AGENT_TIMEOUT_SEC", "3600"))
QEMU_TIMEOUT = int(_get_env("KAG_QEMU_TIMEOUT_SEC", "600"))
BUILD_TIMEOUT = int(_get_env("KAG_BUILD_TIMEOUT_SEC", "900"))

# ─── Docker ─────────────────────────────────────────────────────────
DOCKER_COMPOSE = _get_env("KAG_DOCKER_COMPOSE", "")
VOLUME_CCACHE = _get_env("KAG_VOLUME_CCACHE", "kag-ccache")


# ─── Kernel ─────────────────────────────────────────────────────────
ARCH = _get_env("KAG_ARCH", "arm64")
LLVM = _get_env("KAG_LLVM", "1") == "1"

# ─── QEMU ───────────────────────────────────────────────────────────
QEMU_ACCEL = _get_env("KAG_QEMU_ACCEL", "tcg")
QEMU_CPU = _get_env("KAG_QEMU_CPU", "max")


def get_llm_config(role: str) -> dict:
    """Get LLM configuration for a specific agent role."""
    role_upper = role.upper()
    return {
        "model": _get_env(f"KAG_LLM_MODEL_{role_upper}", "deepseek/deepseek-chat"),
        "max_tokens": int(_get_env(f"KAG_LLM_MAX_TOKENS_{role_upper}", "4000")),
        "fallback": _get_env(f"KAG_LLM_FALLBACK_{role_upper}") or None,
    }
