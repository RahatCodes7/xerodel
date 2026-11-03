(function () {
  var code = window.ORDER_CODE;
  if (!code) return;
  var badge = document.getElementById('status-badge');
  var credsCard = document.getElementById('credentials-card');
  var credsEl = document.getElementById('credentials');
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&':'&','<':'<','>':'>','"':'"',"'":'''}[c]; }); }
  function renderCredentials(creds) {
    if (!credsEl) return;
    credsEl.innerHTML = creds.map(function (c) {
      var html = '<div class="cred"><div class="cred-title">' + esc(c.product_title) + '</div>';
      html += '<div class="cred-row"><span>Username</span><code>' + esc(c.username) + '</code></div>';
      html += '<div class="cred-row"><span>Password</span><code>' + esc(c.password) + '</code></div>';
      if (c.cookie) html += '<div class="cred-row"><span>Cookie</span><code class="cookie">' + esc(c.cookie) + '</code></div>';
      html += '</div>';
      return html;
    }).join('');
  }
  var terminal = false;
  function poll() {
    fetch('/api/orders/' + encodeURIComponent(code)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || d.error) return;
      if (badge) badge.textContent = d.status_badge;
      if (d.status === 'delivered') {
        if (credsCard) credsCard.style.display = '';
        if (d.credentials) renderCredentials(d.credentials);
        terminal = true;
      } else if (d.status === 'cancelled' || d.status === 'rejected') {
        terminal = true;
      }
    }).catch(function () {});
  }
  var timer = setInterval(function () { if (terminal) { clearInterval(timer); return; } poll(); }, 5000);
})();