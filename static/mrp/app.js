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
