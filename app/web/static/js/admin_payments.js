(function () {
  var pill = document.getElementById('pending-count');
  if (!pill) return;
  function poll() {
    fetch('/api/admin/payments/pending').then(function (r) { return r.json(); }).then(function (d) {
      var n = (d.pending || []).length;
      pill.textContent = n > 0 ? n : '';
      pill.style.display = n > 0 ? '' : 'none';
    }).catch(function () {});
  }
  poll();
  setInterval(poll, 15000);
})();