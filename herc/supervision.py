"""Check the process watchdog before opening task hardware."""

import os


def require_watchdog():
    """Require systemd supervision with a timeout of at most 15 seconds."""
    try:
        interval = int(os.environ.get("WATCHDOG_USEC", "0"))
        owner = int(os.environ.get("WATCHDOG_PID", str(os.getpid())))
    except ValueError as exc:
        raise RuntimeError("Invalid systemd watchdog settings") from exc
    if (
        not os.environ.get("NOTIFY_SOCKET")
        or not 0 < interval <= 15_000_000
        or owner != os.getpid()
    ):
        raise RuntimeError(
            "Run under the documented systemd watchdog of at most 15 seconds"
        )
