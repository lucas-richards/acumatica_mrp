// Tell assistive technology the plan is reloading while a vendor filter request runs.
document.addEventListener('htmx:beforeRequest', function (event) {
  if (event.detail.target && event.detail.target.id === 'plan-content') {
    event.detail.target.setAttribute('aria-busy', 'true');
  }
});
document.addEventListener('htmx:afterRequest', function (event) {
  if (event.detail.target && event.detail.target.id === 'plan-content') {
    event.detail.target.removeAttribute('aria-busy');
  }
});

// Expand/collapse the finished-goods breakdown row under a component.
// HTMX loads the row content on the first click; later clicks only toggle it.
document.addEventListener('click', function (event) {
  var button = event.target.closest('[data-toggle-row]');
  if (!button) return;
  var row = document.getElementById(button.getAttribute('aria-controls'));
  if (!row) return;
  var open = button.getAttribute('aria-expanded') === 'true';
  button.setAttribute('aria-expanded', open ? 'false' : 'true');
  row.hidden = open;
});

// "Refresh from Acumatica" starts a background sync; show the overlay until the page
// comes back with the "Refreshing…" banner, which then tracks progress.
document.addEventListener('submit', function (event) {
  var form = event.target.closest('[data-refresh-form]');
  var overlay = document.getElementById('plan-loading');
  if (!form || !overlay) return;
  overlay.querySelector('[data-loading-text]').textContent = 'Starting refresh from Acumatica';
  overlay.classList.add('is-active');
  var button = form.querySelector('button[type="submit"]');
  if (button) {
    button.disabled = true;
    button.textContent = 'Refreshing…';
  }
});

// If the browser restores this page from its back/forward cache, don't leave the overlay up.
window.addEventListener('pageshow', function (event) {
  if (!event.persisted) return;
  var overlay = document.getElementById('plan-loading');
  if (overlay) overlay.classList.remove('is-active');
  document.querySelectorAll('[data-refresh-form] button[type="submit"]').forEach(function (button) {
    button.disabled = false;
    button.textContent = 'Refresh from Acumatica';
  });
});
