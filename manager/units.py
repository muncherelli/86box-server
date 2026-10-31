"""Start/stop box units through systemctl (polkit allows only box@, vnc@, box-settings@ and caddy reload)."""
import subprocess


def run(verb, unit):
    """Return (ok, stderr)."""
    r = subprocess.run(["systemctl", "--no-ask-password", verb, unit],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0, r.stderr.strip()


def is_active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0
