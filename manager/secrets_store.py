"""Secrets the manager needs (for now only the Cloudflare API token).

<data_root>/manager/secrets.json, mode 0600, owned by the manager user:
    {"cf_api_token": {"value": "...", "set": "2026-11-02T06:00:00Z"}}
Values never go to the browser in full, to logs or to the audit log. Not encrypted at rest: the stdlib has no
AES, and a key on the same disk would add little. Protection is the file mode and the separate user.
"""
import datetime
import os
import threading

import config
import store

KNOWN = ("cf_api_token",)
_lock = threading.Lock()


def _path():
    return os.path.join(config.CFG["data_root"], "manager", "secrets.json")


def _load():
    data = store.read_json(_path(), {})
    return data if isinstance(data, dict) else {}


def get(name):
    """The secret's value, or '' when not set."""
    entry = _load().get(name)
    return entry.get("value", "") if isinstance(entry, dict) else ""


def info(name):
    """What the page may show: {"set": iso, "last4": "abcd"} or None. Never the value."""
    entry = _load().get(name)
    if not isinstance(entry, dict) or not entry.get("value"):
        return None
    return {"set": entry.get("set", ""), "last4": entry["value"][-4:]}


def put(name, value, user="system", ip="-"):
    if name not in KNOWN:
        raise ValueError("unknown secret")
    value = value.strip()
    if not value or len(value) > 4096 or any(c.isspace() for c in value):
        raise ValueError("the value must be one line with no spaces")
    with _lock:
        data = _load()
        existed = name in data
        data[name] = {"value": value,
                      "set": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        store.write_json(_path(), data, mode=0o600)
    store.audit(user, ip, "secret", "%s %s" % (name, "replaced" if existed else "set"))


def delete(name, user="system", ip="-"):
    with _lock:
        data = _load()
        if name not in data:
            return
        del data[name]
        store.write_json(_path(), data, mode=0o600)
    store.audit(user, ip, "secret", "%s removed" % name)
