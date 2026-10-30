"""Settings button: hardware is changed in 86Box's own Settings window, shown in the VM's console."""
import re

import config
import respond
import units
import vms

N = r"([a-z][a-z0-9-]{1,30})"


def _groups_html(name):
    groups = vms.all_defs()[name].get("allowed_groups", [])
    if not groups:
        return "<p>This VM may not use any network group, so network cards cannot be attached.</p>"
    items = "".join("<li>%s (bridge <code>%s</code>)</li>" % (
        respond.esc(g), respond.esc(config.CFG["netgroups"].get(g, {}).get("bridge", "?"))) for g in groups)
    return ("<p>Network cards must be of type <b>tap</b> (called &ldquo;TAP&rdquo; or &ldquo;Bridge&rdquo; in 86Box) and use one of "
            "these bridges. Anything else stops the VM within a few seconds.</p><ul>%s</ul>" % items)


def _allowed(req, name):
    if name not in vms.all_defs() or not vms._auth().can_use_vm(req.user, name):
        return False
    return True


def page(req, m):
    name = m.group(1)
    if not _allowed(req, name):
        return respond.text("not found", 404)
    q = respond.esc(name)
    console = respond.esc(vms.console_url(name))
    if units.is_active("vm@%s.service" % name):
        body = ('<p>This VM is running. Open the <a href="%s" target="_blank" rel="noopener">console</a>, then use the 86Box menu '
                '<b>Tools &gt; Settings&hellip;</b>. Saving the settings resets the VM.</p>' % console)
    elif units.is_active("vm-settings@%s.service" % name):
        body = '<p>The settings window is open in the <a href="%s" target="_blank" rel="noopener">console</a>. Close it to finish.</p>' % console
    else:
        body = ('<p>This VM is stopped. The Settings window opens on its own in the console; closing it ends the session.</p>'
                '<form method="post" action="/manage/vms/%s/settings/open"><input type="hidden" name="csrf" value="%s">'
                '<input type="submit" value="Open Settings"></form>' % (q, respond.esc(req.session["csrf"])))
    return respond.page(req, "Settings for " + name, body + _groups_html(name))


def open_settings(req, m):
    name = m.group(1)
    if not _allowed(req, name):
        return respond.text("not found", 404)
    if units.is_active("vm@%s.service" % name):
        return respond.redirect("/manage/vms/%s/settings" % name)
    if not units.is_active("vm-settings@%s.service" % name):
        ok, err = units.run("start", "vm-settings@%s.service" % name)
        if not ok:
            return respond.page(req, "Settings for " + name, '<p class="error">%s</p>' % respond.esc(err), 500)
    return respond.redirect(vms.console_url(name))


ROUTES = [
    ("GET", r"/manage/vms/%s/settings" % N, page, "admin"),
    ("POST", r"/manage/vms/%s/settings/open" % N, open_settings, "admin"),
]
