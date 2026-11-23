"""A team evidence record; hashing cannot prove physical tests were performed."""

import hashlib
import json
from pathlib import Path

REQUIRED_TESTS = (
    "power_load",
    "pan_repeatability",
    "camera_range",
    "radio_reception",
    "remote_arm",
    "fault_response",
    "clock_offline",
    "results_display",
    "dress_rehearsal",
)


def fingerprint(config_path):
    path = Path(config_path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    canonical = dict(cfg)
    for key in ("excursion_id", "clock_check_utc"):
        canonical.pop(key, None)
    hashes = {
        "configuration": hashlib.sha256(
            json.dumps(canonical, sort_keys=True).encode()
        ).hexdigest()
    }
    base = Path(__file__).resolve().parents[1]
    files = (
        list((base / "herc").glob("*.py"))
        + list((base / "tools").glob("*.py"))
        + list((base / "deploy").glob("*.conf"))
    )
    for item in files:
        hashes[str(item.relative_to(base)).replace("\\", "/")] = hashlib.sha256(
            item.read_bytes()
        ).hexdigest()
    for key in ("pan_calibration", "camera_calibration"):
        item = (path.parent / cfg[key]).resolve()
        hashes[key] = hashlib.sha256(item.read_bytes()).hexdigest()
    return hashes


def verify(config_path, record_path):
    data = json.loads(Path(record_path).read_text(encoding="utf-8"))
    if data.get("fingerprint") != fingerprint(config_path):
        raise ValueError(
            "Configuration, calibration or source changed since commissioning"
        )
    operators = data.get("operators", [])
    if (
        not isinstance(operators, list)
        or any(not isinstance(name, str) or not name.strip() for name in operators)
        or len({name.strip().casefold() for name in operators}) < 2
    ):
        raise ValueError("Two named team verifiers are required")
    for key in REQUIRED_TESTS:
        test = data.get("tests", {}).get(key, {})
        if test.get("passed") is not True or not str(test.get("record", "")).strip():
            raise ValueError("Missing measured acceptance evidence for " + key)
    return data
