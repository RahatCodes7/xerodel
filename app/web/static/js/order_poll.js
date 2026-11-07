(function () {
  var code = window.ORDER_CODE;
  if (!code) return;
  var badge = document.getElementById('status-badge');
  var credsCard = document.getElementById('credentials-card');
  var credsEl = document.getElementById('credentials');
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function renderCredentials(creds) {
    if (!credsEl) return;
    credsEl.innerHTML = creds.map(function (c) {
      var html = '<div class="cred"><div class="cred-title">' + esc(c.product_title) + '</div>';
      html += '<div class="cred-row"><span>Username</span><code>' + esc(c.username) + '</code></div>';
      html += '<div class="cred-row"><span>Password</span><code>' + esc(c.password) + '</code></div>';
      if (c.cookie_url) html += '<div class="cred-row"><span>Cookie</span><a class="btn btn-sm btn-primary" href="' + esc(c.cookie_url) + '" download>Download .txt</a></div>';
      html += '</div>';
      return html;
    }).join('');
  }
  function showCookieGuide(creds) {
    var guide = document.getElementById('cookie-guide');
    if (!guide) return;
    var names = (creds || []).filter(function (c) { return c.cookie; }).map(function (c) { return c.username; });
    if (!names.length) return;
    guide.style.display = '';
    var nodes = guide.querySelectorAll('.js-cookie-usernames');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = names.join(', ');
  }
  var terminal = false;
  function poll() {
    fetch('/api/orders/' + encodeURIComponent(code)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || d.error) return;
      if (badge) badge.textContent = d.status_badge;
      if (d.status === 'delivered') {
        if (credsCard) credsCard.style.display = '';
        if (d.credentials) renderCredentials(d.credentials);
        showCookieGuide(d.credentials);
        terminal = true;
      } else if (d.status === 'cancelled' || d.status === 'rejected') {
        terminal = true;
      }
    }).catch(function () {});
  }
  var timer = setInterval(function () { if (terminal) { clearInterval(timer); return; } poll(); }, 5000);
})();