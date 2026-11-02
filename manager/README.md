# manager

Web app for one 86server host. Python standard library only (Python 3.13+, so no `cgi`, no YAML).
Runs as the `86boxmgr` user on `127.0.0.1:8686`; Caddy is the only thing that talks to it.

## Module contract

`server.py` loads these optional modules from this folder, if they exist: `auth vms media snapshots stats sites secrets_ui importexp clone settings`.
Each module may define `ROUTES = [(method, regex, handler, role), ...]`.

- `method`: `GET`, `POST` or `PUT` (there is no DELETE: deletes are `POST .../delete`).
- `regex`: matched with `re.fullmatch` against the path (no query string).
- `role`: `None` (public), `"any"` (logged in), `"admin"`, or `"login"` (public, and no CSRF check; only for `POST /manage/login`).
- `handler(req, match)` returns `(status, headers, body_bytes)`. Build it with the helpers in `respond.py`.

`req` (see `Request` in `server.py`): `method path query headers ip user session`, and `req.form()`, `req.json()`, `req.body()` for small bodies.
Big uploads read `req.rfile` directly, exactly `req.length` bytes.
`req.user` is `{"name", "role", "vms"}` and `req.session` is `{"csrf", ...}`, both set by `auth.authenticate(req)`; both are `None` on public routes.

### What `auth.py` must provide (used by `server.py` and other modules)

- `authenticate(req)` -> user dict or `None`; sets `req.user` and `req.session`.
- `csrf_ok(req)` -> bool: the `X-CSRF` header or the `csrf` form field equals the session token.
- `console_users()` -> `{name: {"vms": [...]}}` for users with role `console`.
- `set_vm_users(vm, usernames)` -> make exactly these console users own `vm` (rewrites users.json).
- `remove_vm(vm)` -> remove `vm` from every user's `vms` list.
- `can_use_vm(user, vm)` -> True for admins, or when `vm` is in the user's list.

### Other modules

- `config.CFG`: settings dict (`data_root`, `manager_host`, `emu86box_tag`, ...). Tests set `B86_DATA_ROOT`.
- `config.netgroups()` and `config.netpolicy()`: the network groups and group pairs as the root helper applied them
  (read live from `/opt/86server/etc/netgroups.json` and `netpolicy.json`; never read `CFG["netgroups"]`).
  Tests point `config.NETGROUPS_PATH`, `NETPOLICY_PATH` and `NETAPPLY_RESULT` at a temp folder.
- `netgroups_store.py` / `netgroups_ui.py`: the Network groups page. The manager has no network privileges, so a change
  is a complete desired state written to `<data_root>/manager/netgroups-request.json` (`{id, netgroups, group_routes,
  nat_dns}`). The root helper applies it and answers in `/run/86server/netapply.json` (`{id, ok, message,
  warnings, ts}`). `GET /manage/api/netgroups/status` reports applying / ok / error / idle (30 s timeout).
  `tools/fake-netapply.py` is a test-only stand-in for the helper.
- `store.write_json / write_text / read_json / audit`: always write files with these (atomic, explicit mode).
- `units.run(verb, unit)` and `units.is_active(unit)`: tests patch these; never call systemctl directly.
- `respond.page(req, title, body_html)` wraps a page in `pages/layout.html`. Escape every value with `respond.esc`.
  Forms need `<input type="hidden" name="csrf" value="...">` with `req.session["csrf"]`.

## Tests

`cd manager && python3 -m unittest discover -s tests -t .`
