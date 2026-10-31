"""Host and per-box CPU, RAM and disk numbers, refreshed every 5 s by one background thread."""
import glob
import os
import re
import shutil
import subprocess
import threading
import time

import config
import respond
import units

PROC = "/proc"
CGROUP = "/sys/fs/cgroup"
VM_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
INTERVAL = 5
DU_EVERY = 60

_lock = threading.Lock()
_data = {"host": {}, "vms": {}}
_started = False


def parse_cpu(text):
    """First line of /proc/stat -> (busy, total) jiffies."""
    v = [int(x) for x in text.splitlines()[0].split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v) - idle, sum(v)


def parse_meminfo(text):
    m = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+) kB", text, re.M)}
    return m.get("MemTotal", 0) * 1024, m.get("MemAvailable", 0) * 1024


def _read(path):
    with open(path) as f:
        return f.read()


def cgroup_dir(unit):
    r = subprocess.run(["systemctl", "show", "-p", "ControlGroup", "--value", unit],
                       capture_output=True, text=True, timeout=10)
    path = r.stdout.strip()
    return CGROUP + path if path else None


def usage_usec(cg):
    return int(re.search(r"^usage_usec (\d+)", _read(os.path.join(cg, "cpu.stat")), re.M).group(1))


def vm_names():
    root = os.path.join(config.CFG["data_root"], "boxdefs")
    return [n for n in (os.path.basename(p)[:-5] for p in sorted(glob.glob(root + "/*.json"))) if VM_RE.fullmatch(n)]


class Sampler:
    def __init__(self):
        self.last_cpu = None
        self.last_vm = {}   # name -> (usage_usec, time)
        self.du = {}        # name -> (bytes, time)

    def sample(self, now=None):
        now = now if now is not None else time.monotonic()
        host = {}
        busy, total = parse_cpu(_read(PROC + "/stat"))
        if self.last_cpu and total > self.last_cpu[1]:
            host["cpu_percent"] = round(100.0 * (busy - self.last_cpu[0]) / (total - self.last_cpu[1]), 1)
        else:
            host["cpu_percent"] = 0.0
        self.last_cpu = (busy, total)
        mem_total, mem_avail = parse_meminfo(_read(PROC + "/meminfo"))
        host["ram_total"], host["ram_used"] = mem_total, mem_total - mem_avail
        d = shutil.disk_usage(config.CFG["data_root"])
        host["disk_total"], host["disk_used"] = d.total, d.used

        vms = {}
        for name in vm_names():
            info = {"active": units.is_active("box@%s.service" % name)}
            if info["active"]:
                try:
                    cg = cgroup_dir("box@%s.service" % name)
                    usec = usage_usec(cg)
                    prev = self.last_vm.get(name)
                    info["cpu_percent"] = round(100.0 * (usec - prev[0]) / ((now - prev[1]) * 1e6), 1) if prev and now > prev[1] else 0.0
                    self.last_vm[name] = (usec, now)
                    info["ram"] = int(_read(os.path.join(cg, "memory.current")))
                except (OSError, AttributeError, subprocess.SubprocessError):
                    info["cpu_percent"], info["ram"] = 0.0, 0
            else:
                self.last_vm.pop(name, None)
            cached = self.du.get(name)
            if not cached or now - cached[1] >= DU_EVERY:
                r = subprocess.run(["du", "-sb", os.path.join(config.CFG["data_root"], "boxes", name)],
                                   capture_output=True, text=True)
                self.du[name] = (int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout else 0, now)
            info["disk"] = self.du[name][0]
            vms[name] = info
        with _lock:
            _data["host"], _data["vms"] = host, vms


def _loop():
    s = Sampler()
    while True:
        try:
            s.sample()
        except Exception as e:  # keep sampling whatever goes wrong
            print("stats error: %r" % e, flush=True)
        time.sleep(INTERVAL)


def start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True).start()


def get(user=None):
    """Current numbers; console users only see their own boxes."""
    start()
    with _lock:
        vms = dict(_data["vms"])
        host = dict(_data["host"])
    if user and user.get("role") != "admin":
        vms = {k: v for k, v in vms.items() if k in user.get("vms", [])}
    return {"host": host, "vms": vms}


def api(req, m):
    return respond.json_resp(get(req.user))


ROUTES = [("GET", r"/manage/api/stats", api, "any")]
