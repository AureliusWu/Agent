from __future__ import annotations

import json
import sys


REQUIRED_VERSION = (3, 12)


def main() -> int:
    actual = sys.version_info[:2]
    payload = {
        "status": "passed" if actual == REQUIRED_VERSION else "failed",
        "required": ".".join(map(str, REQUIRED_VERSION)),
        "actual": ".".join(map(str, actual)),
        "executable": sys.executable,
    }
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if actual == REQUIRED_VERSION else 1


if __name__ == "__main__":
    raise SystemExit(main())
