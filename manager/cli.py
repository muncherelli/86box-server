"""Terminal fallback for the web manager. Run as the 86server user.

list | start|stop|restart <name> | check-password <user> | set-password <user> | renew-certs
The password commands read the password from stdin, so it never shows up in the process list.
"""
import sys

import auth
import vms

ACTIONS = {"start": "start", "stop": "stop", "restart": "reset"}


def cmd_list():
    rows = [vms.status(name) for name in sorted(vms.all_defs())]
    print("%-20s %-9s %-8s %s" % ("NAME", "STATE", "NIC", "NOTE"))
    for r in rows:
        print("%-20s %-9s %-8s %s" % (r["name"], r["state"], r["nic"], r["guard"]))
    return 0


def cmd_action(verb, name):
    if name not in vms.all_defs():
        print("no such box: %s" % name, file=sys.stderr)
        return 1
    ok, err = vms.do_action(name, ACTIONS[verb])
    if not ok:
        print(err or "failed", file=sys.stderr)
        return 1
    print("%s: %s ok" % (name, verb))
    return 0


def _stdin_password():
    return sys.stdin.readline().rstrip("\n")


def cmd_check_password(name):
    user = auth._load()["users"].get(name)
    if not user or not auth.verify_password(_stdin_password(), user["hash"]):
        print("password does not match for %s" % name, file=sys.stderr)
        return 1
    print("password ok for %s" % name)
    return 0


def cmd_set_password(name):
    """Signs the user out in sessions.json; the caller restarts the manager so the running one forgets them too."""
    auth._load_sessions()  # otherwise saving would drop every other user's saved session
    try:
        auth.set_password(name, _stdin_password())
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("password set for %s" % name)
    return 0


def main(argv):
    if argv == ["list"]:
        return cmd_list()
    if len(argv) == 2 and argv[0] in ACTIONS:
        return cmd_action(argv[0], argv[1])
    if len(argv) == 2 and argv[0] == "check-password":
        return cmd_check_password(argv[1])
    if len(argv) == 2 and argv[0] == "set-password":
        return cmd_set_password(argv[1])
    if argv == ["renew-certs"]:
        import panel
        return panel.renew()
    print("usage: cli.py list | start|stop|restart <name> | check-password <user> | set-password <user>"
          " | renew-certs",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
