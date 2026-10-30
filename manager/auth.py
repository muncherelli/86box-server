"""Login for the manager: scrypt passwords, in-memory sessions, CSRF, per-IP rate limit.

Users live in <data_root>/manager/users.json (mode 0600). Sessions are in memory only,
so a manager restart logs everyone out. The one exception is dev mode (./update.sh dev),
which keeps them in sessions.json so a restart on every save doesn't log you out.
See README.md for the API other modules use.
"""
import collections
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse

import config
import respond
import store

COOKIE = "b86s"
IDLE_SECONDS = 12 * 3600
ABSOLUTE_SECONDS = 7 * 24 * 3600
FAIL_LIMIT = 5
FAIL_WINDOW = 15 * 60
USER_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")
VM_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 16384, 8, 1

_users_lock = threading.Lock()    # guards every read-modify-write of users.json
_sess_lock = threading.Lock()     # guards SESSIONS and FAILS
SESSIONS = {}                     # token -> {token, user, csrf, created, last}
FAILS = {}                        # ip -> deque of failure times
_dummy = None


class AuthError(Exception):
    """A message that is safe to show to the admin."""


# ---------- passwords ----------

def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, salt.hex(), h.hex())


def verify_password(pw, stored):
    try:
        _, n, r, p, salt, want = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), want)
    except (ValueError, AttributeError):
        return False


def check_password_rules(pw):
    if not 12 <= len(pw) <= 256:
        raise AuthError("Password must be 12 to 256 characters.")


# ---------- users.json ----------

def _users_path():
    return os.path.join(config.CFG["data_root"], "manager", "users.json")


def _load():
    return store.read_json(_users_path(), {"users": {}})


def _save(data):
    store.write_json(_users_path(), data, mode=0o600)


def _admin_count(users):
    return sum(1 for u in users.values() if u["role"] == "admin")


def _clean_vms(vms):
    out = []
    for v in vms:
        if not VM_RE.fullmatch(v):
            raise AuthError("Bad VM name: %s" % v)
        if v not in out:
            out.append(v)
    return out


def create_user(name, pw, role, vms=()):
    if not USER_RE.fullmatch(name):
        raise AuthError("Username: lowercase letters, digits, - and _, 2 to 32 characters, starting with a letter.")
    if role not in ("admin", "console"):
        raise AuthError("Role must be admin or console.")
    check_password_rules(pw)
    vms = _clean_vms(vms)
    with _users_lock:
        data = _load()
        if name in data["users"]:
            raise AuthError("That user already exists.")
        data["users"][name] = {"hash": hash_password(pw), "role": role, "vms": vms}
        _save(data)


def delete_user(name):
    with _users_lock:
        data = _load()
        u = data["users"].get(name)
        if u is None:
            raise AuthError("No such user.")
        if u["role"] == "admin" and _admin_count(data["users"]) <= 1:
            raise AuthError("You can't delete the last admin.")
        del data["users"][name]
        _save(data)
    delete_user_sessions(name)


def set_role_vms(name, role, vms):
    if role not in ("admin", "console"):
        raise AuthError("Role must be admin or console.")
    vms = _clean_vms(vms)
    with _users_lock:
        data = _load()
        u = data["users"].get(name)
        if u is None:
            raise AuthError("No such user.")
        if u["role"] == "admin" and role != "admin" and _admin_count(data["users"]) <= 1:
            raise AuthError("You can't demote the last admin.")
        u["role"], u["vms"] = role, vms
        _save(data)
    delete_user_sessions(name)


def set_password(name, pw):
    check_password_rules(pw)
    with _users_lock:
        data = _load()
        if name not in data["users"]:
            raise AuthError("No such user.")
        data["users"][name]["hash"] = hash_password(pw)
        _save(data)
    delete_user_sessions(name)


def seed_if_needed():
    """First start: create the admin from seed.json (written by Ansible), then delete the seed."""
    seed = os.path.join(config.CFG["data_root"], "manager", "seed.json")
    if os.path.exists(_users_path()) or not os.path.exists(seed):
        return
    info = store.read_json(seed, {})
    try:
        create_user(info.get("admin_user", "admin"), info.get("admin_password", ""), "admin")
    except AuthError as e:
        print("seed.json not used: %s" % e, file=sys.stderr, flush=True)
        return
    os.unlink(seed)
    store.audit("system", "-", "seed-admin", info.get("admin_user", "admin"))


def init():
    seed_if_needed()
    _load_sessions()


# ---------- API for other modules ----------

def console_users():
    return {n: {"vms": u["vms"]} for n, u in _load()["users"].items() if u["role"] == "console"}


def set_vm_users(vm, usernames):
    """Make exactly these console users own vm."""
    changed = []
    with _users_lock:
        data = _load()
        for n, u in data["users"].items():
            if u["role"] != "console":
                continue
            has = vm in u["vms"]
            want = n in usernames
            if want and not has:
                u["vms"].append(vm)
            elif has and not want:
                u["vms"].remove(vm)
            else:
                continue
            changed.append(n)
        if changed:
            _save(data)
    for n in changed:
        delete_user_sessions(n)


def remove_vm(vm):
    changed = []
    with _users_lock:
        data = _load()
        for n, u in data["users"].items():
            if vm in u["vms"]:
                u["vms"].remove(vm)
                changed.append(n)
        if changed:
            _save(data)
    for n in changed:
        delete_user_sessions(n)


def can_use_vm(user, vm):
    return user["role"] == "admin" or vm in user["vms"]


# ---------- sessions, rate limit ----------

_saved_at = 0.0


def _save_sessions(now, force=True):
    """Dev mode only. The caller holds _sess_lock. Idle refreshes are saved at most once a minute."""
    global _saved_at
    if not respond.dev_mode() or (not force and now - _saved_at < 60):
        return
    _saved_at = now
    try:
        store.write_json(os.path.join(config.CFG["data_root"], "manager", "sessions.json"), SESSIONS, mode=0o600)
    except OSError as e:
        print("sessions.json not saved: %s" % e, file=sys.stderr, flush=True)


def _load_sessions():
    """Dev mode: reload saved sessions. Otherwise remove a leftover file so no tokens stay on disk."""
    path = os.path.join(config.CFG["data_root"], "manager", "sessions.json")
    if not respond.dev_mode():
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    saved = store.read_json(path, {})
    with _sess_lock:
        for t, s in (saved.items() if isinstance(saved, dict) else []):
            if isinstance(s, dict) and all(k in s for k in ("token", "user", "csrf", "created", "last")):
                SESSIONS[t] = s


def create_session(username, now=None):
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    with _sess_lock:
        SESSIONS[token] = {"token": token, "user": username, "csrf": secrets.token_urlsafe(16),
                           "created": now, "last": now}
        _save_sessions(now)
    return token


def lookup_session(token, now=None):
    now = time.time() if now is None else now
    with _sess_lock:
        for t in [t for t, s in SESSIONS.items()
                  if now - s["last"] > IDLE_SECONDS or now - s["created"] > ABSOLUTE_SECONDS]:
            del SESSIONS[t]
        s = SESSIONS.get(token)
        if s:
            s["last"] = now
            _save_sessions(now, force=False)
        return s


def delete_session(token):
    with _sess_lock:
        SESSIONS.pop(token, None)
        _save_sessions(time.time())


def delete_user_sessions(username):
    with _sess_lock:
        for t in [t for t, s in SESSIONS.items() if s["user"] == username]:
            del SESSIONS[t]
        _save_sessions(time.time())


def rate_blocked(ip, now=None):
    now = time.time() if now is None else now
    with _sess_lock:
        q = FAILS.get(ip)
        if q is None:
            return False
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        return len(q) >= FAIL_LIMIT


def rate_fail(ip, now=None):
    now = time.time() if now is None else now
    with _sess_lock:
        FAILS.setdefault(ip, collections.deque()).append(now)


def login(ip, username, password, now=None):
    """Return ("ok", token), ("bad", None) or ("blocked", None)."""
    global _dummy
    if rate_blocked(ip, now):
        return "blocked", None
    user = _load()["users"].get(username)
    if user is None:
        if _dummy is None:
            _dummy = hash_password("not-a-real-password")
        verify_password(password, _dummy)   # same timing as a real check
        ok = False
    else:
        ok = verify_password(password, user["hash"])
    if not ok:
        rate_fail(ip, now)
        return "bad", None
    return "ok", create_session(username, now)


# ---------- hooks used by server.py ----------

def _cookie_token(req):
    for part in (req.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


def authenticate(req):
    token = _cookie_token(req)
    sess = lookup_session(token) if token else None
    if sess is None:
        return None
    u = _load()["users"].get(sess["user"])
    if u is None:
        delete_session(sess["token"])
        return None
    req.session = sess
    req.user = {"name": sess["user"], "role": u["role"], "vms": u["vms"]}
    return req.user


def csrf_ok(req):
    got = req.headers.get("X-CSRF")
    if got is None and (req.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
        got = req.form().get("csrf")
    return bool(got) and hmac.compare_digest(got, req.session["csrf"])


# ---------- pages ----------

def _safe_next(value):
    if value and value.startswith("/manage/") and "//" not in value:
        return value
    return "/manage/"


def _csrf_field(req):
    return '<input type="hidden" name="csrf" value="%s">' % respond.esc(req.session["csrf"])


def _login_page(req, error="", status=200):
    nxt = _safe_next((req.query.get("next") or [""])[0])
    err = '<p class="error">%s</p>' % respond.esc(error) if error else ""
    body = (err + '<form method="post" action="/manage/login">'
            '<input type="hidden" name="next" value="%s">'
            '<p><label>User <input name="username" autocomplete="username" required></label></p>'
            '<p><label>Password <input type="password" name="password" autocomplete="current-password" required></label></p>'
            '<p><input type="submit" value="Log in"></p></form>') % respond.esc(nxt)
    return respond.page(req, "Log in", body, status)


def login_get(req, m):
    return _login_page(req)


def login_post(req, m):
    form = req.form()
    result, token = login(req.ip, form.get("username", ""), form.get("password", ""))
    if result == "blocked":
        store.audit(form.get("username", ""), req.ip, "login-blocked")
        return respond.page(req, "Log in", '<p class="error">Too many failed attempts. Try again in a few minutes.</p>', 429)
    if result == "bad":
        store.audit(form.get("username", ""), req.ip, "login-fail")
        return _login_page(req, "Wrong user or password.", 401)
    store.audit(form["username"], req.ip, "login-ok")
    status, headers, body = respond.redirect(_safe_next(form.get("next")))
    headers["Set-Cookie"] = "%s=%s; Path=/; HttpOnly; Secure; SameSite=Strict" % (COOKIE, token)
    return status, headers, body


def logout_post(req, m):
    store.audit(req.user["name"], req.ip, "logout")
    delete_session(req.session["token"])
    status, headers, body = respond.redirect("/manage/login")
    headers["Set-Cookie"] = "%s=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0" % COOKIE
    return status, headers, body


def _account_page(req, error="", status=200):
    err = '<p class="error">%s</p>' % respond.esc(error) if error else ""
    body = (err + '<form method="post" action="/manage/account">%s'
            '<p><label>Current password <input type="password" name="current" required></label></p>'
            '<p><label>New password <input type="password" name="new" required></label></p>'
            '<p><input type="submit" value="Change password"></p></form>'
            '<form method="post" action="/manage/logout">%s<input type="submit" value="Log out"></form>'
            ) % (_csrf_field(req), _csrf_field(req))
    return respond.page(req, "Account", body, status)


def account_get(req, m):
    return _account_page(req)


def account_post(req, m):
    form = req.form()
    name = req.user["name"]
    if rate_blocked(req.ip):
        return _account_page(req, "Too many failed attempts. Try again in a few minutes.", 429)
    if not verify_password(form.get("current", ""), _load()["users"][name]["hash"]):
        rate_fail(req.ip)
        store.audit(name, req.ip, "password-change-fail")
        return _account_page(req, "Current password is wrong.", 400)
    try:
        set_password(name, form.get("new", ""))
    except AuthError as e:
        return _account_page(req, str(e), 400)
    store.audit(name, req.ip, "password-change")
    return respond.redirect("/manage/login")   # all sessions of this user are gone


def _users_page(req, error="", status=200):
    esc, csrf = respond.esc, _csrf_field(req)
    rows = []
    for n, u in sorted(_load()["users"].items()):
        sel = lambda r: " selected" if u["role"] == r else ""
        rows.append(
            '<tr><td>%s</td><td><form method="post" action="/manage/users/update">%s'
            '<input type="hidden" name="name" value="%s">'
            '<select name="role"><option%s>admin</option><option%s>console</option></select> '
            'VMs <input name="vms" value="%s"> <input type="submit" value="Save"></form></td>'
            '<td><form method="post" action="/manage/users/password">%s<input type="hidden" name="name" value="%s">'
            '<input type="password" name="password" placeholder="new password"> <input type="submit" value="Set"></form></td>'
            '<td><form method="post" action="/manage/users/delete">%s<input type="hidden" name="name" value="%s">'
            '<input type="submit" value="Delete"></form></td></tr>'
            % (esc(n), csrf, esc(n), sel("admin"), sel("console"), esc(" ".join(u["vms"])),
               csrf, esc(n), csrf, esc(n)))
    err = '<p class="error">%s</p>' % esc(error) if error else ""
    body = (err + "<table><tr><th>User</th><th>Role and VMs</th><th>Password</th><th></th></tr>" + "".join(rows) + "</table>"
            '<h2>Add user</h2><form method="post" action="/manage/users/add">' + csrf +
            '<p><input name="name" placeholder="username" required> '
            '<input type="password" name="password" placeholder="password (12+ characters)" required> '
            '<select name="role"><option>console</option><option>admin</option></select> '
            'VMs <input name="vms"> <input type="submit" value="Add"></p></form>')
    return respond.page(req, "Users", body, status)


def users_get(req, m):
    return _users_page(req)


def users_post(req, m):
    action, form = m.group(1), req.form()
    name = form.get("name", "")
    vms = form.get("vms", "").replace(",", " ").split()
    try:
        if action == "add":
            create_user(name, form.get("password", ""), form.get("role", ""), vms)
        elif action == "delete":
            delete_user(name)
        elif action == "update":
            set_role_vms(name, form.get("role", ""), vms)
        else:
            set_password(name, form.get("password", ""))
    except AuthError as e:
        return _users_page(req, str(e), 400)
    store.audit(req.user["name"], req.ip, "user-" + action, name)
    return respond.redirect("/manage/users")


def auth_check(req, m):
    """Caddy forward_auth for /websockify: 200 only if the user may open the console named in ?token=."""
    parts = urllib.parse.urlsplit(req.headers.get("X-Forwarded-Uri", ""))
    tokens = urllib.parse.parse_qs(parts.query).get("token", [])
    if parts.path != "/websockify" or len(tokens) != 1 or not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", tokens[0]):
        return 403, {"Content-Type": "text/plain"}, b"forbidden"
    user = authenticate(req)
    if user is None:
        return 401, {"Content-Type": "text/plain"}, b"login required"
    if not can_use_vm(user, tokens[0]):
        return 403, {"Content-Type": "text/plain"}, b"forbidden"
    return 200, {"Content-Type": "text/plain"}, b"ok"


ROUTES = [
    ("GET", r"/auth/check", auth_check, None),
    ("GET", r"/manage/login", login_get, None),
    ("POST", r"/manage/login", login_post, "login"),
    ("POST", r"/manage/logout", logout_post, "any"),
    ("GET", r"/manage/account", account_get, "any"),
    ("POST", r"/manage/account", account_post, "any"),
    ("GET", r"/manage/users", users_get, "admin"),
    ("POST", r"/manage/users/(add|delete|update|password)", users_post, "admin"),
]
