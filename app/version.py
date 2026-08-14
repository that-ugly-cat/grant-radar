"""Deployed version shown in the footer: GIT_COMMIT env (set at Docker build)
or a live git call in development."""
import os
import subprocess


def commit_hash() -> str:
    env = os.environ.get("GIT_COMMIT", "").strip()
    if env:
        return env[:12]
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"
