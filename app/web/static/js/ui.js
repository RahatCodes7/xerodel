(function () {
  var toasts = document.getElementById('toasts') || (function () {
    var el = document.createElement('div');
    el.id = 'toasts';
    el.style.position = 'fixed';
    el.style.bottom = '24px';
    el.style.right = '24px';
    el.style.zIndex = '1000';
    el.style.display = 'flex';
    el.style.flexDirection = 'column';
    el.style.gap = '10px';
    document.body.appendChild(el);
    return el;
  })();

  window.showToast = function (text, type) {
    if (!type) type = 'info';
    var colors = { success: '#22c55e', error: '#ef4444', info: '#3b82f6', warn: '#f59e0b' };
    var d = document.createElement('div');
    d.style.background = '#111827';
    d.style.border = '1px solid rgba(255,255,255,0.1)';
    d.style.borderLeft = '4px solid ' + colors[type];
    d.style.color = '#fff';
    d.style.padding = '12px 16px';
    d.style.borderRadius = '10px';
    d.style.fontSize = '14px';
    d.style.boxShadow = '0 10px 30px -10px rgba(0,0,0,0.5)';
    d.style.animation = 'slideIn 0.3s ease';
    d.innerHTML = '<span style="font-weight:600; margin-right:8px;">' + type.toUpperCase() + '</span>' + text;
    toasts.appendChild(d);
    setTimeout(function () {
      d.style.opacity = '0';
      d.style.transform = 'translateX(20px)';
      d.style.transition = 'all 0.3s';
      setTimeout(function () { d.remove(); }, 300);
    }, 3500);
  };

  window.copyToClipboard = function (text, btn) {
    navigator.clipboard.writeText(text).then(function () {
      showToast('Copied to clipboard', 'success');
      if (btn) {
        var old = btn.innerText;
        btn.innerText = 'Copied!';
        btn.style.background = '#22c55e';
        btn.style.borderColor = '#22c55e';
        setTimeout(function () {
          btn.innerText = old;
          btn.style.background = '';
          btn.style.borderColor = '';
        }, 1500);
      }
    }).catch(function () {
      showToast('Copy failed', 'error');
    });
  };
})();

var slideIn = document.createElement('style');
slideIn.textContent = '@keyframes slideIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }';
document.head.appendChild(slideIn);