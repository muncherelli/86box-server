"""Terminal fallback for the web manager: list | start|stop|restart <name>. Run as the 86boxmgr user."""
import sys

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


def main(argv):
    if argv == ["list"]:
        return cmd_list()
    if len(argv) == 2 and argv[0] in ACTIONS:
        return cmd_action(argv[0], argv[1])
    print("usage: cli.py list | start|stop|restart <name>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
