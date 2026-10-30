"""Small file helpers: atomic writes and the audit log."""
import datetime
import json
import os

import config


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_text(path, text, mode=0o660):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json(path, data, mode=0o660):
    write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode)


def audit(user, ip, action, detail=""):
    """Append one JSON line to audit.log. Never pass passwords or tokens."""
    line = json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user": user, "ip": ip, "action": action, "detail": detail,
    })
    path = os.path.join(config.CFG["data_root"], "manager", "audit.log")
    with open(path, "a") as f:
        f.write(line + "\n")
