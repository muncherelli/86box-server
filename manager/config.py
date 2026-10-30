"""Settings: /etc/86box-server/manager.json (written by Ansible). Tests set B86_CONFIG / B86_DATA_ROOT."""
import json
import os

_PATH = os.environ.get("B86_CONFIG", "/etc/86box-server/manager.json")


def load():
    try:
        with open(_PATH) as f:
            cfg = json.load(f)
    except OSError:
        cfg = {}
    cfg.setdefault("data_root", "/var/lib/86box-server")
    cfg.setdefault("emu86box_tag", "")
    cfg.setdefault("netgroups", {})
    cfg.setdefault("edge_mode", "acme")
    cfg.setdefault("manager_host", "localhost")
    cfg.setdefault("publish_domains", [])
    cfg.setdefault("port_forward_limits", [])
    cfg.setdefault("public_ipv4", "")
    if os.environ.get("B86_DATA_ROOT"):
        cfg["data_root"] = os.environ["B86_DATA_ROOT"]
    return cfg


CFG = load()
