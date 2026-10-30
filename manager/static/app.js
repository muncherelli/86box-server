// Small helpers shared by pages. Page-specific code is added by later rows.
function csrfToken() {
  var m = document.querySelector('meta[name="csrf"]');
  return m ? m.content : "";
}
