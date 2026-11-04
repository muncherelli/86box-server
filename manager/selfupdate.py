"""Update notice and Update button (admins). Ansible records each deploy in deployed.json; a unit running as the
clone's owner compares the clone's HEAD with it (check.json) and, when an admin presses Update, runs
`86server update` (run.json). The manager only reads those files and writes the request the path unit watches.
See ansible/roles/selfupdate."""
import datetime
import json
import os

import config
import respond
import store

RUN_STALE = 2 * 3600  # a "running" state older than this is left over from a crash


def _read(path):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _state(name):
    return _read(os.path.join(config.CFG["update_state_dir"], name))


def deployed():
    return _read(config.CFG["deployed_file"])


def _request_path():
    return os.path.join(config.CFG["data_root"], "manager", "update-request.json")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _age(iso):
    try:
        return (_now() - datetime.datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def asset_version():
    """Cache key for /manage/static links: changes with every deploy."""
    d = deployed()
    v = (d.get("short") or "") + (d.get("at") or "")
    return "".join(c for c in v if c.isalnum()) or "0"


def status():
    dep, chk, run = deployed(), _state("check.json"), _state("run.json")
    if run.get("state") == "running" and (_age(run.get("started_at")) or 0) > RUN_STALE:
        run = dict(run, state="failed", output="The update stopped without finishing (see the update log).")
    # A check made against an older deploy says nothing about this one.
    fresh = bool(dep.get("commit")) and chk.get("deployed") == dep.get("commit") and not chk.get("error")
    return {
        "enabled": bool(dep) and os.path.isdir(config.CFG["update_state_dir"]),
        "deployed": {k: dep.get(k) for k in ("short", "subject", "branch", "dirty", "at")},
        "available": fresh and bool(chk.get("available")),
        "count": chk.get("count") if fresh else 0,
        "diverged": fresh and bool(chk.get("diverged")),
        "commits": (chk.get("commits") or []) if fresh else [],
        "checked_at": chk.get("checked_at"),
        "check_error": chk.get("error", ""),
        "run": {k: run.get(k) for k in ("state", "started_at", "finished_at", "rc", "output", "log")} if run else None,
    }


def badge(req):
    """Header markup for admins: the notice (when there is something to install, or an update is running) and
    its dialog. Empty for everyone else."""
    if not req.user or req.user.get("role") != "admin":
        return ""
    st = status()
    running = (st["run"] or {}).get("state") == "running"
    if not st["enabled"] or not (st["available"] or running):
        return ""
    e = respond.esc
    if running:
        label = "Updating"
    elif st["diverged"]:
        label = "Update available"
    else:
        n = st["count"] or 0
        label = "Update available · %d new commit%s" % (n, "" if n == 1 else "s")
    rows = "".join(
        '<li><code>%s</code> <span>%s</span></li>' % (e(c.get("short", "")), e(c.get("subject", "")))
        for c in st["commits"])
    more = ""
    if (st["count"] or 0) > len(st["commits"]):
        more = '<p class="hint">and %d more</p>' % (st["count"] - len(st["commits"]))
    dep = st["deployed"]
    note = ('<p class="hint">The clone is not on top of what is installed (reset, rebase or another branch). '
            'Updating installs its current state.</p>' if st["diverged"] else "")
    return (
        '<button type="button" class="update-badge%s" id="update-badge" aria-haspopup="dialog">'
        '<span class="led" aria-hidden="true"></span>%s</button>'
        '<dialog id="update-dialog" class="update-dialog" data-running="%s">'
        '<h2>Update 86server</h2>'
        '<p>Installed: <code>%s</code>%s %s</p>'
        '<p>New in the clone (<code>%s</code>):</p><ul class="commits">%s</ul>%s%s'
        '<p class="hint">Runs <code>86server update</code> on the server. The panel restarts on the way and this '
        'page reloads when it is back.</p>'
        '<p class="update-progress" id="update-progress" hidden></p>'
        '<pre class="update-output" id="update-output" hidden></pre>'
        '<div class="actions"><button type="button" value="cancel" id="update-close">Close</button>'
        '<button type="button" class="primary" id="update-go">Install update</button></div>'
        '</dialog>'
    ) % (" running" if running else "", e(label), "1" if running else "",
         e(dep.get("short") or "?"), " (with uncommitted changes)" if dep.get("dirty") else "",
         e(dep.get("subject") or ""), e(dep.get("branch") or ""), rows, more, note)


def h_status(req, m):
    return respond.json_resp(status())


def h_update(req, m):
    st = status()
    if not st["enabled"]:
        return respond.json_resp({"ok": False, "error": "updates from the panel are not set up; run 86server update"}, 409)
    if (st["run"] or {}).get("state") == "running":
        return respond.json_resp({"ok": False, "error": "an update is already running"}, 409)
    at = _now().isoformat(timespec="seconds")
    store.write_text(_request_path(), json.dumps({"at": at, "by": req.user["name"]}) + "\n", mode=0o600)
    return respond.json_resp({"ok": True, "requested_at": at})


ROUTES = [
    ("GET", r"/manage/api/update", h_status, "admin"),
    ("POST", r"/manage/api/update", h_update, "admin"),
]
