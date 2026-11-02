// Cloudflare page: while a set-up or remove job runs, poll its status and reload the page when it ends.
(function () {
  var job = document.getElementById("cf-job");
  if (!job || job.getAttribute("data-running") !== "1") return;
  function poll() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/manage/api/cloudflare/status");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.onload = function () {
      var st = null;
      try { st = JSON.parse(xhr.responseText); } catch (e) {}
      if (st && !st.running) { location.reload(); return; }
      if (st && st.steps) {
        var cur = st.steps.filter(function (s) { return s.state === "running"; })[0];
        job.textContent = cur ? "Working: " + cur.name + "..." : "Working...";
      }
      setTimeout(poll, 2000);
    };
    xhr.onerror = function () { setTimeout(poll, 5000); };
    xhr.send();
  }
  setTimeout(poll, 2000);
})();
