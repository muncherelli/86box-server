// Small helpers shared by pages. Page-specific code is added by later rows.

// Theme switcher in the menu: System (follows the OS setting, the default), Light or Dark.
// The choice is kept in this browser only. The <head> script in layout.html applies it before first paint.
(function () {
  var sel = document.getElementById("theme-switch");
  if (!sel) return;
  var saved = "system";
  try { saved = localStorage.getItem("b86-theme") || "system"; } catch (e) {}
  sel.value = saved === "light" || saved === "dark" ? saved : "system";
  sel.addEventListener("change", function () {
    var v = sel.value;
    if (v === "light" || v === "dark") document.documentElement.setAttribute("data-theme", v);
    else document.documentElement.removeAttribute("data-theme");
    try {
      if (v === "system") localStorage.removeItem("b86-theme");
      else localStorage.setItem("b86-theme", v);
    } catch (e) {}
  });
})();
// Mark the menu link for the current section.
(function () {
  var path = location.pathname.replace(/\/+$/, "");
  var best = null;
  document.querySelectorAll(".main-nav a").forEach(function (a) {
    var href = a.getAttribute("href").replace(/\/+$/, "");
    var hit = href === "/manage" ? (path === "/manage" || path.indexOf("/manage/boxes") === 0)
                                 : (path === href || path.indexOf(href + "/") === 0);
    if (hit && (!best || href.length > best.getAttribute("href").length)) best = a;
  });
  if (best) best.setAttribute("aria-current", "page");
})();

function csrfToken() {
  var m = document.querySelector('meta[name="csrf"]');
  return m ? m.content : "";
}

// box list: 5 s status poll and Start/Stop/Reset buttons.
(function () {
  var table = document.getElementById("vm-table");
  if (!table) return;
  var dlg = document.getElementById("confirm");
  var pending = null;

  function poll() {
    fetch("/manage/api/boxes").then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      d.vms.forEach(function (v) {
        var row = table.querySelector('tr[data-vm="' + v.name + '"]');
        if (!row) return;
        var st = row.querySelector("[data-state]");
        st.textContent = v.state;
        st.dataset.state = v.state;
        row.querySelector("[data-nic]").textContent = v.nic;
        row.querySelector("[data-guard]").textContent = v.guard;
      });
    });
  }

  function mb(n) { return n >= 1073741824 ? (n / 1073741824).toFixed(1) + " GB" : Math.round(n / 1048576) + " MB"; }

  function pollStats() {
    fetch("/manage/api/stats").then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      Object.keys(d.vms || {}).forEach(function (name) {
        var row = table.querySelector('tr[data-vm="' + name + '"]');
        var v = d.vms[name];
        if (!row) return;
        row.querySelector("[data-cpu]").textContent = v.active ? v.cpu_percent + "%" : "";
        row.querySelector("[data-ram]").textContent = v.active ? mb(v.ram) : "";
        row.querySelector("[data-disk]").textContent = mb(v.disk);
      });
    });
  }

  function send(vm, action) {
    fetch("/manage/api/box/" + vm + "/" + action, { method: "POST", headers: { "X-CSRF": csrfToken() } })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (!d.ok) alert(d.error || "failed"); poll(); });
  }

  table.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-action]");
    if (!b) return;
    var action = b.dataset.action, vm = b.dataset.vm;
    if (action === "start") return send(vm, action);
    pending = { vm: vm, action: action };
    document.getElementById("confirm-text").textContent =
      "This is like pulling the plug. Shut the guest OS down first if you can. " + action + " " + vm + "?";
    dlg.showModal();
  });
  document.getElementById("confirm-yes").onclick = function () {
    dlg.close();
    if (pending) send(pending.vm, pending.action);
  };
  document.getElementById("confirm-no").onclick = function () { dlg.close(); };
  setInterval(poll, 5000);
  pollStats();
  setInterval(pollStats, 5000);
})();

// Dev mode only (86server dev): reload the page after the manager restarts.
(function () {
  if (!document.querySelector('meta[name="dev-reload"]')) return;
  var seen = null;
  setInterval(function () {
    fetch("/manage/dev-id", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (id) {
        if (!id) return;
        if (seen === null) seen = id;
        else if (id !== seen) location.reload();
      })
      .catch(function () {});
  }, 1000);
})();

// Network groups: wait while the root helper applies a change, then show the result.
(function () {
  if (!document.getElementById("net-apply")) return;
  var timer = setInterval(function () {
    fetch("/manage/api/netgroups/status", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || d.state === "applying") return;
        clearInterval(timer);
        location.href = "/manage/netgroups" + (d.state === "ok" ? "?done=1" : "");
      })
      .catch(function () {});
  }, 1000);
})();
