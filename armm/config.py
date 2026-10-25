#!/usr/bin/env python3
"""ARMM shared config — paths and the ~/armm/.env reader/writer.

Secrets live ONLY in ~/armm/.env (chmod 600). Never in the repo, never in git,
never in argv. This module is the single place that touches that file.
"""
import os, tempfile

ARMM_HOME = os.path.expanduser(os.environ.get("ARMM_HOME", "~/armm"))
ENV_PATH = os.path.join(ARMM_HOME, ".env")
SPECS_PATH = os.path.join(ARMM_HOME, "specs.json")
DB_PATH = os.path.join(ARMM_HOME, "armm.db")
LOG_PATH = os.path.join(ARMM_HOME, "bot.log")
GUARD_STATE_PATH = os.path.join(ARMM_HOME, "guard_state.json")


def ensure_home():
    os.makedirs(ARMM_HOME, mode=0o700, exist_ok=True)
    return ARMM_HOME


def load_env(path=None):
    """Parse KEY=VALUE lines. Missing file -> {} (callers decide what is fatal)."""
    path = path or ENV_PATH
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        return {}
    return env


def env_get(key, default=None, path=None):
    """~/armm/.env first, then the process environment (systemd EnvironmentFile)."""
    v = load_env(path).get(key)
    if v:
        return v
    return os.environ.get(key) or default


def set_env_value(key, value, path=None):
    """Write KEY=value into .env atomically, preserving 0600 and other keys."""
    path = path or ENV_PATH
    ensure_home()
    lines, found = [], False
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        pass
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key and not line.strip().startswith("#"):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(path, 0o600)
    return path


def secret_fragments():
    """Values that must never reach a log line or a transcript."""
    env = load_env()
    out = []
    for k in ("WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE", "TELEGRAM_BOT_TOKEN",
              "ANTHROPIC_API_KEY"):
        v = env.get(k) or os.environ.get(k)
        if v and len(v) >= 8:
            out.append(v)
    return out
