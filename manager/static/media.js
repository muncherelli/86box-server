// Media library page: multi-file upload (raw PUT per file, no multipart), selection, bulk and single delete.
(function () {
  // ---- confirm dialog
  var dlg = document.getElementById("media-confirm");
  var onYes = null;
  function ask(text, yes) {
    if (!dlg || !dlg.showModal) { if (confirm(text)) yes(); return; }
    document.getElementById("media-confirm-text").textContent = text;
    onYes = yes;
    dlg.showModal();
  }
  if (dlg) {
    document.getElementById("media-confirm-no").onclick = function () { dlg.close(); };
    document.getElementById("media-confirm-yes").onclick = function () {
      dlg.close();
      if (onYes) onYes();
    };
  }

  // ---- single delete
  var one = document.getElementById("delete-one");
  var kind = new URLSearchParams(location.search).get("kind") || "iso";
  document.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-delete]");
    if (!b || !one) return;
    var name = b.dataset.delete;
    ask("Delete " + name + "? This cannot be undone.", function () {
      one.action = "/manage/api/media/" + encodeURIComponent(kind) + "/" + encodeURIComponent(name) + "/delete";
      one.elements.confirm.value = name;
      one.submit();
    });
  });

  // ---- selection and bulk delete
  var bulk = document.getElementById("bulk");
  if (bulk) {
    var all = document.getElementById("pick-all");
    var bar = bulk.querySelector(".bulkbar");
    var picked = document.getElementById("picked");
    var boxes = function () { return bulk.querySelectorAll('tbody input[name="name"]:not(:disabled)'); };
    var update = function () {
      var list = boxes(), n = 0;
      list.forEach(function (c) { if (c.checked) n++; });
      bar.hidden = n === 0;
      picked.textContent = n + (n === 1 ? " file selected" : " files selected");
      if (all) {
        all.checked = n > 0 && n === list.length;
        all.indeterminate = n > 0 && n < list.length;
      }
    };
    if (all) all.addEventListener("change", function () {
      boxes().forEach(function (c) { c.checked = all.checked; });
      update();
    });
    bulk.addEventListener("change", function (e) { if (e.target.name === "name") update(); });
    // Shift-click selects a range.
    var last = null;
    bulk.addEventListener("click", function (e) {
      var c = e.target;
      if (c.type !== "checkbox" || c.name !== "name") return;
      var list = Array.prototype.slice.call(boxes());
      if (e.shiftKey && last && last !== c) {
        var a = list.indexOf(last), b = list.indexOf(c);
        list.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(function (x) { x.checked = c.checked; });
        update();
      }
      last = c;
    });
    bulk.addEventListener("submit", function (e) {
      e.preventDefault();
      var n = 0;
      boxes().forEach(function (c) { if (c.checked) n++; });
      if (!n) return;
      ask("Delete " + n + (n === 1 ? " file" : " files") + "? This cannot be undone.", function () {
        bulk.elements.confirm.value = String(n);
        bulk.submit();
      });
    });
    update();
  }

  // ---- page size picker submits on change
  document.querySelectorAll("select[data-autosubmit]").forEach(function (s) {
    s.addEventListener("change", function () { s.form.submit(); });
  });

  // ---- speed and size helpers (uploads and server downloads)
  function size(n) {
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
  }
  function clock(sec) {
    if (!isFinite(sec) || sec < 0) return "";
    sec = Math.round(sec);
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), x = sec % 60;
    return (h ? h + ":" + (m < 10 ? "0" : "") : "") + m + ":" + (x < 10 ? "0" : "") + x;
  }
  // Speed over the last few seconds, so it follows changes without jumping on every event.
  function meter() {
    var samples = [];
    return function (bytes) {
      var now = Date.now();
      samples.push([now, bytes]);
      while (samples.length > 2 && now - samples[1][0] > 4000) samples.shift();
      var a = samples[0], dt = (now - a[0]) / 1000;
      return dt > 0.25 ? (bytes - a[1]) / dt : 0;
    };
  }
  function progressText(done, total, speed) {
    var t = (total ? Math.floor(100 * done / total) + "% · " : "") + size(done) + (total ? " of " + size(total) : "");
    if (speed > 0) {
      t += " · " + size(speed) + "/s";
      if (total) t += " · " + clock((total - done) / speed) + " left";
    }
    return t;
  }

  var queue = document.getElementById("queue");
  function row(label, onCancel) {
    var li = document.createElement("li");
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = label;
    var bar = document.createElement("progress");
    bar.max = 100; bar.value = 0;
    var msg = document.createElement("span");
    msg.className = "hint";
    msg.textContent = "waiting";
    li.appendChild(name); li.appendChild(bar); li.appendChild(msg);
    var stop = null;
    if (onCancel) {
      stop = document.createElement("button");
      stop.type = "button";
      stop.className = "ghost small";
      stop.textContent = "Cancel";
      stop.onclick = function () { stop.disabled = true; onCancel(); };
      li.appendChild(stop);
      li.classList.add("has-cancel");
    }
    queue.appendChild(li);
    return { li: li, bar: bar, msg: msg, name: name, stop: stop };
  }

  // Reload the list once nothing is running and something new arrived.
  var uploading = false, fetching = 0, arrived = 0;
  function maybeReload(kind) {
    if (uploading || fetching || !arrived) return;
    var url = new URL(location.href);
    if (kind) url.searchParams.set("kind", kind);
    url.searchParams.delete("page");
    setTimeout(function () { location.href = url.toString(); }, 800);
  }

  // ---- upload: several files, picked or dropped, sent one after another
  var form = document.getElementById("upload");
  if (form) {
    var drop = document.getElementById("drop");
    var input = form.elements.file;
    var pending = [], lastKind = null;

    var next = function () {
      var item = pending.shift();
      if (!item) { uploading = false; maybeReload(lastKind); return; }
      uploading = true;
      lastKind = item.kind;
      var ui = item.ui, speed = meter(), started = Date.now();
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", "/manage/api/media/" + item.kind + "/" + encodeURIComponent(item.file.name));
      xhr.setRequestHeader("X-CSRF", csrfToken());
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        ui.bar.value = Math.floor(100 * e.loaded / e.total);
        ui.msg.textContent = progressText(e.loaded, e.total, speed(e.loaded));
      };
      // All bytes are sent; the server still writes them to disk before it answers.
      xhr.upload.onload = function () { ui.bar.removeAttribute("value"); ui.msg.textContent = "finishing…"; };
      xhr.onload = function () {
        var secs = (Date.now() - started) / 1000;
        if (xhr.status === 201) {
          arrived++;
          ui.bar.value = 100;
          ui.msg.textContent = "done · " + size(item.file.size) + " at " + size(item.file.size / Math.max(secs, 0.001)) + "/s";
          ui.li.classList.add("ok");
        } else {
          var err = "failed (" + xhr.status + ")";
          try { err = JSON.parse(xhr.responseText).error || err; } catch (x) {}
          ui.bar.value = 0;
          ui.msg.textContent = err;
          ui.li.classList.add("failed");
        }
        next();
      };
      xhr.onerror = function () { ui.msg.textContent = "failed"; ui.li.classList.add("failed"); next(); };
      ui.msg.textContent = "starting";
      xhr.send(item.file);
    };

    var add = function (files) {
      var k = form.elements.kind.value;
      Array.prototype.forEach.call(files, function (f) { pending.push({ file: f, kind: k, ui: row(f.name) }); });
      if (!uploading) next();
    };

    input.addEventListener("change", function () { add(input.files); input.value = ""; });
    ["dragenter", "dragover"].forEach(function (t) {
      drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (t) {
      drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("over"); });
    });
    drop.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) add(e.dataTransfer.files);
    });
    // Leaving cuts an upload off. A server download carries on, so it does not warn.
    window.addEventListener("beforeunload", function (e) {
      if (uploading) { e.preventDefault(); e.returnValue = ""; }
    });
  }

  // ---- download from a link: the server fetches it; this page follows the job
  var fform = document.getElementById("fetch");
  if (!fform) return;
  var jobs = {}, timer = null;
  var LIVE = { queued: 1, connecting: 1, downloading: 1 };

  function cancel(id) {
    fetch("/manage/api/media/fetch/" + id + "/cancel", { method: "POST", headers: { "X-CSRF": csrfToken() } });
  }
  function track(job) {
    if (jobs[job.id]) return;
    jobs[job.id] = { ui: row(job.label || "download", function () { cancel(job.id); }), speed: meter(), live: true };
    fetching++;
    show(job);
  }
  function show(job) {
    var j = jobs[job.id];
    if (!j || !j.live) return;
    var ui = j.ui;
    if (job.name) ui.name.textContent = job.name;
    if (job.state === "queued") { ui.msg.textContent = "waiting for a free slot"; return; }
    if (job.state === "connecting") { ui.msg.textContent = "connecting"; return; }
    if (job.state === "downloading") {
      if (job.total) ui.bar.value = Math.floor(100 * job.bytes / job.total); else ui.bar.removeAttribute("value");
      ui.msg.textContent = progressText(job.bytes, job.total, j.speed(job.bytes));
      return;
    }
    j.live = false;
    fetching--;
    if (ui.stop) ui.stop.remove();
    ui.li.classList.remove("has-cancel");
    if (job.state === "done") {
      arrived++;
      ui.bar.value = 100;
      var secs = Math.max(((job.finished || Date.now() / 1000) - job.started), 0.001);
      ui.msg.textContent = "done · " + size(job.bytes) + " at " + size(job.bytes / secs) + "/s · saved in " + job.kind;
      ui.li.classList.add("ok");
      maybeReload(job.kind);
    } else {
      ui.bar.value = 0;
      ui.msg.textContent = job.state === "cancelled" ? "cancelled" : job.error || "failed";
      ui.li.classList.add("failed");
      maybeReload(null);
    }
  }
  function poll() {
    fetch("/manage/api/media/fetches", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        d.jobs.forEach(function (job) {
          if (!jobs[job.id] && LIVE[job.state]) track(job);  // started before this page loaded
          else show(job);
        });
        if (!fetching && timer) { clearInterval(timer); timer = null; }
      })
      .catch(function () {});
  }
  function watch() { if (!timer) timer = setInterval(poll, 1000); }

  fform.addEventListener("submit", function (e) {
    e.preventDefault();
    var btn = fform.querySelector("button[type=submit]");
    btn.disabled = true;
    var body = new URLSearchParams();
    ["url", "name", "kind"].forEach(function (k) { body.set(k, fform.elements[k].value); });
    fetch("/manage/api/media/fetch", { method: "POST", headers: { "X-CSRF": csrfToken() }, body: body })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        if (!d.ok) {
          var ui = row(fform.elements.url.value);
          ui.bar.value = 0;
          ui.msg.textContent = d.error || "could not start";
          ui.li.classList.add("failed");
          return;
        }
        fform.elements.url.value = "";
        fform.elements.name.value = "";
        track(d.job);
        watch();
      })
      .catch(function () { btn.disabled = false; });
  });
  poll();
  watch();
})();
