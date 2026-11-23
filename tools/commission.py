import argparse
import json
from pathlib import Path
from herc.commissioning import fingerprint, REQUIRED_TESTS, verify
from herc.evidence import atomic_json, utc_now
from herc.controller import validate_live_configuration


def main():
    p = argparse.ArgumentParser(
        description="Record team verification after actual tests"
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--records", required=True)
    p.add_argument("--out", default="commissioning.json")
    a = p.parse_args()
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    validate_live_configuration(cfg)
    record = json.loads(Path(a.records).read_text(encoding="utf-8"))
    if len(set(s.strip() for s in record.get("operators", []) if s.strip())) < 2:
        raise ValueError("Enter two real team verifier names")
    for key in REQUIRED_TESTS:
        result = record.get("tests", {}).get(key, {})
        if (
            result.get("passed") is not True
            or not str(result.get("record", "")).strip()
        ):
            raise ValueError("Missing physical verification: " + key)
    record.update(fingerprint=fingerprint(a.config), recorded_utc=utc_now())
    atomic_json(a.out, record)
    verify(a.config, a.out)
    print(
        "Recorded team verification. This tool cannot independently verify the physical tests."
    )


if __name__ == "__main__":
    main()
