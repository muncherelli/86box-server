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

  // ---- upload: several files, picked or dropped, sent one after another
  var form = document.getElementById("upload");
  if (!form) return;
  var drop = document.getElementById("drop");
  var queue = document.getElementById("queue");
  var input = form.elements.file;
  var busy = false, pending = [], okCount = 0;

  function row(file) {
    var li = document.createElement("li");
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = file.name;
    var bar = document.createElement("progress");
    bar.max = 100; bar.value = 0;
    var msg = document.createElement("span");
    msg.className = "hint";
    msg.textContent = "waiting";
    li.appendChild(name); li.appendChild(bar); li.appendChild(msg);
    queue.appendChild(li);
    return { li: li, bar: bar, msg: msg };
  }

  function next() {
    var item = pending.shift();
    if (!item) {
      busy = false;
      if (okCount) {
        var url = new URL(location.href);
        url.searchParams.set("kind", form.elements.kind.value);
        url.searchParams.delete("page");
        setTimeout(function () { location.href = url.toString(); }, 600);
      }
      return;
    }
    busy = true;
    var ui = item.ui;
    var xhr = new XMLHttpRequest();
    xhr.open("PUT", "/manage/api/media/" + item.kind + "/" + encodeURIComponent(item.file.name));
    xhr.setRequestHeader("X-CSRF", csrfToken());
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        ui.bar.value = Math.round(100 * e.loaded / e.total);
        ui.msg.textContent = ui.bar.value + "%";
      }
    };
    xhr.onload = function () {
      if (xhr.status === 201) {
        okCount++;
        ui.bar.value = 100;
        ui.msg.textContent = "done";
        ui.li.className = "ok";
      } else {
        var err = "failed (" + xhr.status + ")";
        try { err = JSON.parse(xhr.responseText).error || err; } catch (x) {}
        ui.msg.textContent = err;
        ui.li.className = "failed";
      }
      next();
    };
    xhr.onerror = function () { ui.msg.textContent = "failed"; ui.li.className = "failed"; next(); };
    ui.msg.textContent = "0%";
    xhr.send(item.file);
  }

  function add(files) {
    var k = form.elements.kind.value;
    Array.prototype.forEach.call(files, function (f) { pending.push({ file: f, kind: k, ui: row(f) }); });
    if (!busy) next();
  }

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
  window.addEventListener("beforeunload", function (e) {
    if (busy) { e.preventDefault(); e.returnValue = ""; }
  });
})();
