#!/usr/bin/env python3
"""Write a fail2ban status snapshot as JSON (run as root by launchd).

The fail2ban control socket is root-only, so the SrvWatch service (which runs
as a normal user) cannot query it directly. This helper runs as root on a timer
(see deploy/install-launchd.sh install-fail2ban) and writes a small JSON file
that the service reads to show jail/ban counts.

Usage: fail2ban-status.py <output-path>
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

CLIENT = "/opt/homebrew/opt/fail2ban/bin/fail2ban-client"


def run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", str(e)


def main():
    if len(sys.argv) < 2:
        print("usage: fail2ban-status.py <output-path>", file=sys.stderr)
        return 2
    out_path = sys.argv[1]

    client = CLIENT if os.path.exists(CLIENT) else "fail2ban-client"
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "jails": [],
        "banned": 0,
    }

    rc, out, err = run([client, "status"])
    if rc != 0:
        data["error"] = (err or out or "fail2ban-client failed").strip().splitlines()[0][:160]
    else:
        jail_line = ""
        for line in out.splitlines():
            if "Jail list:" in line:
                jail_line = line.split("Jail list:", 1)[1].strip()
        jail_names = [j.strip() for j in jail_line.split(",") if j.strip()]

        total = 0
        for jail in jail_names:
            rc, jout, _ = run([client, "status", jail])
            entry = {"name": jail, "banned": 0, "failed": 0}
            for line in jout.splitlines():
                line = line.strip()
                if line.startswith("Currently banned:"):
                    try:
                        entry["banned"] = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif line.startswith("Currently failed:"):
                    try:
                        entry["failed"] = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
            total += entry["banned"]
            data["jails"].append(entry)
        data["banned"] = total

    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
