// Upload a file to the media library with a progress bar (raw PUT, no multipart).
(function () {
  var form = document.getElementById("upload");
  if (!form) return;
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var file = form.elements.file.files[0];
    var msg = document.getElementById("msg");
    var bar = document.getElementById("bar");
    if (!file) { msg.textContent = "Choose a file first."; return; }
    var xhr = new XMLHttpRequest();
    xhr.open("PUT", "/manage/api/media/" + form.elements.kind.value + "/" + encodeURIComponent(file.name));
    xhr.setRequestHeader("X-CSRF", csrfToken());
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) { bar.hidden = false; bar.value = Math.round(100 * e.loaded / e.total); }
    };
    xhr.onload = function () {
      if (xhr.status === 201) { location.reload(); return; }
      var err = "upload failed (" + xhr.status + ")";
      try { err = JSON.parse(xhr.responseText).error || err; } catch (x) {}
      msg.textContent = err;
    };
    xhr.onerror = function () { msg.textContent = "upload failed"; };
    msg.textContent = "";
    xhr.send(file);
  });
})();
