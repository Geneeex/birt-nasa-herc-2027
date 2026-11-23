"""Generate user services for this installed path. Does not change system files."""

import getpass
import json
from pathlib import Path


def generate(root):
    root = Path(root).resolve()
    # systemd service paths require escaped spaces; avoid that fragile installation layout.
    if any(c.isspace() for c in str(root)):
        raise ValueError(
            "On the Pi, install this package at a path without spaces, e.g. ~/HERC_task"
        )
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    results = (root / cfg.get("results_directory", "runs")).resolve()
    root_text, results_text = root.as_posix(), results.as_posix()
    # Do not silently point the display at a different evidence directory.
    # Refuse characters requiring systemd escaping rather than generate a
    # misleading or expanded ExecStart command.
    for value in (root_text, results_text):
        if any(c.isspace() or c in '%"\\' for c in value):
            raise ValueError(
                "Service paths must contain no spaces, quotes, backslashes or percent signs"
            )
    out = root / "deploy/generated"
    out.mkdir(parents=True, exist_ok=True)
    commands = {
        "herc-receiver": (
            "simple",
            f"/usr/bin/python3 -m tools.receive --config {root_text}/config.json",
            "Restart=on-failure\nRestartSec=2",
        ),
        "herc-review": (
            "simple",
            f"/usr/bin/python3 -m herc.review --root {results_text} --host 127.0.0.1",
            "Restart=on-failure\nRestartSec=2",
        ),
        "herc-controller": (
            "notify",
            f"/usr/bin/python3 -m herc.controller --config {root_text}/config.json",
            "NotifyAccess=main\nWatchdogSec=15\nTimeoutStartSec=90\nTimeoutStopSec=2\nRestart=no",
        ),
    }
    for name, (kind, command, extra) in commands.items():
        text = f"""[Unit]
Description=HERC {name}
After=network.target

[Service]
Type={kind}
WorkingDirectory={root_text}
ExecStart={command}
Environment=PYTHONUNBUFFERED=1
{extra}

[Install]
WantedBy=default.target
"""
        (out / (name + ".service")).write_text(text, encoding="utf-8")
    return out


def main():
    root = Path(__file__).resolve().parents[1]
    out = generate(root)
    print("Created service files in", out)
    print("After qualification: mkdir -p ~/.config/systemd/user")
    print("cp deploy/generated/*.service ~/.config/systemd/user/")
    print("systemctl --user daemon-reload")
    print("systemctl --user enable herc-receiver herc-review herc-controller")
    print("sudo loginctl enable-linger " + getpass.getuser())
    print(
        "Reboot only during pit/bench setup; inspect every service and the physical no-motion state."
    )


if __name__ == "__main__":
    main()
