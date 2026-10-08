(function () {
  try {
    var t = localStorage.getItem('gnubook-theme');
    if (!t) { t = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
    document.documentElement.setAttribute('data-bs-theme', t);
  } catch (e) { /* storage blocked: keep default */ }
})();
