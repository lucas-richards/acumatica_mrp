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

// The graph icon and the finished-goods button under an item open the same detail row: the finished goods
// the item affects on the left (if any) and its two-year inventory chart on the right. Opening it loads
// both the first time; later clicks only show and hide it.
function syncExpanded(detailId, open) {
  document.querySelectorAll('[data-toggle-row][aria-controls="' + detailId + '"]').forEach(function (b) {
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
}
document.addEventListener('click', function (event) {
  var button = event.target.closest('[data-toggle-row]');
  if (!button) return;
  var row = document.getElementById(button.getAttribute('aria-controls'));
  if (!row) return;
  var open = row.hidden;                       // hidden now, so this click opens it
  row.hidden = !open;
  syncExpanded(row.id, open);
  if (open) {
    var fg = row.querySelector('.detail-fg');
    if (fg && window.htmx && !fg._requested) { fg._requested = true; window.htmx.trigger(fg, 'load-detail'); }
    var pane = row.querySelector('[data-history-pane]');
    if (pane && window.openHistory) { window.openHistory(pane); }
  }
});
// A row swapped in by htmx (a usage chosen, a warning marked done) starts closed: match its detail row
document.addEventListener('htmx:afterSettle', function (event) {
  var row = event.detail && event.detail.target && event.detail.target.closest && event.detail.target.closest('tr[data-sku]');
  var detail = row && row.nextElementSibling;
  if (detail && detail.classList.contains('breakdown-row')) { syncExpanded(detail.id, !detail.hidden); }
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

// "Months left with this order": recalculated in the row as the order quantity is typed.
function updateWithOrder(event) {
  var box = event.target.closest && event.target.closest('.qty-input');
  if (!box) { return; }
  var row = box.closest('tr');
  var cell = row && row.querySelector('.js-with-order');
  if (!cell) { return; }
  var usage = parseFloat(row.dataset.usage);
  var qty = parseFloat(box.value.replace(/,/g, ''));
  cell.textContent = '';
  if (row.dataset.need) {            // no lead time and/or no demand: nothing to calculate
    var need = document.createElement('span');
    need.className = 'muted need';
    need.textContent = 'Need ' + row.dataset.need;
    var why = document.createElement('span');
    why.className = 'sub';
    why.textContent = 'to calculate';
    cell.appendChild(need);
    cell.appendChild(why);
    return;
  }
  if (!(usage > 0)) { cell.innerHTML = '<span class="muted">\u2014</span>'; return; }
  if (isNaN(qty) || qty < 0) { qty = 0; }
  var months = (parseFloat(row.dataset.stock) + qty) / usage - parseFloat(row.dataset.lead);
  var text = (Math.round(months * 10) / 10).toLocaleString('en-US', {minimumFractionDigits: 1, maximumFractionDigits: 1});
  var main = document.createElement('span');
  main.className = months < 0 ? 'text-danger strong' : 'strong';
  main.textContent = text + ' mo';
  cell.appendChild(main);
  if (months < 0) {
    var sub = document.createElement('span');
    sub.className = 'sub';
    sub.textContent = 'still runs out before it arrives';
    cell.appendChild(sub);
  }
}
['input', 'change', 'keyup'].forEach(function (name) { document.addEventListener(name, updateWithOrder); });

// Hover (or keyboard focus) explanations: any element with data-tip shows its text in a small box.
(function () {
  var tip = null;
  function show(el) {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'tip';
      tip.setAttribute('role', 'tooltip');
      document.body.appendChild(tip);
    }
    tip.textContent = el.getAttribute('data-tip');
    tip.hidden = false;
    var r = el.getBoundingClientRect();
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8));
    var top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) { top = Math.max(8, r.top - h - 8); }
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function hide() { if (tip) { tip.hidden = true; } }
  document.addEventListener('mouseover', function (e) { var el = e.target.closest('[data-tip]'); if (el) { show(el); } });
  document.addEventListener('mouseout', function (e) { if (e.target.closest('[data-tip]')) { hide(); } });
  document.addEventListener('focusin', function (e) { var el = e.target.closest('[data-tip]'); if (el) { show(el); } });
  document.addEventListener('focusout', hide);
  document.addEventListener('scroll', hide, true);
}());

// Column filters in the table header: item text and status. Rows that do not match are hidden
// (with their expanded breakdown); nothing is reloaded.
(function () {
  function apply() {
    var table = document.querySelector('.plan-table');
    if (!table) { return; }
    var wanted = {};
    table.querySelectorAll('[data-filter]').forEach(function (el) { wanted[el.dataset.filter] = el.value.trim().toLowerCase(); });
    var rows = table.querySelectorAll('tbody tr[data-search]');
    var shown = 0;
    rows.forEach(function (row) {
      var ok = (!wanted.item || row.dataset.search.indexOf(wanted.item) !== -1) &&
               (!wanted.status || (wanted.status === 'back ordered' ? row.dataset.backordered === '1'
                                                                    : row.dataset.status.toLowerCase() === wanted.status));
      row.hidden = !ok;
      var detail = row.nextElementSibling;
      if (detail && detail.classList.contains('breakdown-row')) {
        var toggle = row.querySelector('[data-toggle-row]');
        detail.hidden = !ok || !(toggle && toggle.getAttribute('aria-expanded') === 'true');
      }
      if (ok) { shown += 1; }
    });
    var count = document.getElementById('filter-count');
    if (count) { count.textContent = 'Showing ' + shown + ' of ' + rows.length; }
  }
  ['input', 'change'].forEach(function (name) {
    document.addEventListener(name, function (event) {
      if (event.target.closest && event.target.closest('.col-filter')) { apply(); }
    });
  });
  document.addEventListener('DOMContentLoaded', apply);
  document.addEventListener('htmx:afterSettle', apply);     // a row that was swapped in keeps the filters
}());

// Choosing another usage: the rounded highlight glides from the old value to the new one while the row
// is being swapped, so it reads as one thing moving rather than two cells changing.
(function () {
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var DURATION = 380;

  function marker(cell) { return cell.querySelector('.custom-input') || cell.querySelector('.usage-num'); }

  function glide(row, target) {
    var from = row.querySelector('.usage-cell.is-used');
    if (reduced || !from || from === target) { return; }
    var a = marker(from), b = marker(target);
    if (!a || !b) { return; }
    var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    var ghost = document.createElement('div');
    ghost.className = 'usage-ghost';
    ghost.style.left = ra.left + 'px'; ghost.style.top = ra.top + 'px';
    ghost.style.width = ra.width + 'px'; ghost.style.height = ra.height + 'px';
    document.body.appendChild(ghost);
    from.classList.add('is-leaving');
    void ghost.offsetWidth;
    ghost.style.left = rb.left + 'px'; ghost.style.top = rb.top + 'px';
    ghost.style.width = rb.width + 'px'; ghost.style.height = rb.height + 'px';
    var sku = row.getAttribute('data-sku');
    var done = function () {
      ghost.remove();
      delete pending[sku];
      var fresh = document.querySelector('tr[data-sku="' + (window.CSS && CSS.escape ? CSS.escape(sku) : sku) + '"] .usage-cell.is-used');
      if (fresh) { fresh.classList.remove('is-arriving'); }
    };
    pending[sku] = Date.now() + DURATION;
    setTimeout(done, DURATION);
  }

  var pending = {};
  // The new row's own highlight stays hidden until the moving one has arrived
  document.addEventListener('htmx:afterSwap', function (event) {
    var row = event.detail && event.detail.elt && event.detail.elt.closest && event.detail.elt.closest('tr[data-sku]');
    var swapped = event.detail && event.detail.target && event.detail.target.closest && event.detail.target.closest('tr[data-sku]');
    row = swapped || row;
    if (row && pending[row.getAttribute('data-sku')] > Date.now()) {
      var used = row.querySelector('.usage-cell.is-used');
      if (used) { used.classList.add('is-arriving'); }      // the timer above clears it once the ghost lands
    }
  });

  document.addEventListener('click', function (event) {
    var cell = event.target.closest && event.target.closest('.usage-cell[hx-post]');
    if (cell && !event.target.closest('input')) { glide(cell.closest('tr'), cell); }
  }, true);
  document.addEventListener('change', function (event) {
    var box = event.target.closest && event.target.closest('.custom-input');
    if (box && box.value.trim() !== '') { glide(box.closest('tr'), box.closest('.usage-cell')); }
  }, true);
}());

// Two year inventory chart under an item. The numbers come from the planner app's own endpoint (same
// server), so they match its Inventory Level tab. Chart.js is loaded the first time a chart is opened.
(function () {
  var chartLoad = null, cache = {};
  var TIMEOUT = 60000;

  function loadChartLibrary(src) {
    if (window.Chart) { return Promise.resolve(); }
    if (!chartLoad) {
      chartLoad = new Promise(function (resolve, reject) {
        var script = document.createElement('script');
        script.src = src;
        script.onload = resolve;
        script.onerror = function () { chartLoad = null; reject(new Error('the chart library did not load')); };
        document.head.appendChild(script);
      });
    }
    return chartLoad;
  }

  function note(pane, text, isError) {
    var box = pane.querySelector('.history-note');
    if (!box) { return; }
    box.textContent = text || '';
    box.classList.toggle('is-error', !!isError);
  }

  function fetchHistory(url, sku) {
    if (cache[sku]) { return Promise.resolve(cache[sku]); }
    var controller = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) { controller.abort(); } }, TIMEOUT);
    var target = url + (url.indexOf('?') === -1 ? '?' : '&') + 'sku=' + encodeURIComponent(sku);
    return fetch(target, {credentials: 'same-origin', signal: controller ? controller.signal : undefined})
      .then(function (response) {
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        return response.json();
      })
      .then(function (data) { clearTimeout(timer); cache[sku] = data; return data; },
            function (error) { clearTimeout(timer); throw error; });
  }

  // The endpoint matches by prefix, so several items can come back; take the one for this row.
  function pickRow(data, sku) {
    var wanted = String(sku).trim().toUpperCase();
    var rows = (data && data.inventory) || [];
    for (var i = 0; i < rows.length; i += 1) {
      if (String(rows[i].sku || '').trim().toUpperCase() === wanted) { return rows[i]; }
    }
    return null;
  }

  function draw(pane, row, sku) {
    var adjusted = row.qty || [];
    var rawValues = row.raw_qty || adjusted;
    var spikes = (row.adjustments || []).length;
    // On the printed requisition the axis text is black and bold so it stays readable on paper
    var onSheet = !!pane.closest('.sheet');
    var tick = onSheet ? '#000' : '#666', grid = onSheet ? 'rgba(0,0,0,.25)' : 'rgba(0,0,0,.1)';
    var tickFont = onSheet ? {weight: '600', size: 11} : {};
    var datasets = [{label: sku, data: adjusted, borderColor: onSheet ? '#000' : '#1d2740', borderWidth: onSheet ? 2 : 1.6, pointRadius: 0,
                    pointHoverRadius: 4, tension: 0.1, fill: false}];
    if (onSheet) {
      datasets.push({label: 'Qty to order', data: [], borderColor: '#c62828', borderWidth: 2, borderDash: [8, 5],
                     pointRadius: 0, pointHoverRadius: 0, fill: false});
    }
    var chart = new window.Chart(pane.querySelector('canvas'), {
      type: 'line',
      data: {
        labels: row.dates || [],
        datasets: datasets
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: {mode: 'index', intersect: false},
        plugins: {legend: {display: onSheet, labels: {color: '#000', font: {weight: '600'}}}},
        scales: {x: {ticks: {maxTicksLimit: 10, autoSkip: true, color: tick, font: tickFont}, grid: {color: grid}},
                 y: {beginAtZero: true, ticks: {color: tick, font: tickFont}, grid: {color: grid}}}
      }
    });
    pane._chart = chart;
    if (onSheet) { window.setOrderLine(pane); }
    var toggle = pane.querySelector('[data-history-raw]');
    if (spikes && toggle) {
      toggle.hidden = false;
      toggle.dataset.raw = 'false';
      toggle.textContent = 'Show raw data';
      note(pane, spikes + ' temporary transaction spike' + (spikes === 1 ? '' : 's') + ' left out of the line.');
      pane._series = {values: adjusted, raw: rawValues};
    } else {
      note(pane, '');
    }
  }

  // A dashed horizontal line at the quantity typed in the requisition's order box
  window.setOrderLine = function (pane) {
    var chart = pane && pane._chart, box = pane && pane.closest('.sheet') && pane.closest('.sheet').querySelector('[data-report-qty]');
    if (!chart || !box || chart.data.datasets.length < 2) { return; }
    var qty = parseFloat(String(box.value).replace(/,/g, '')) || 0;
    var line = chart.data.datasets[1];
    line.data = qty > 0 ? chart.data.labels.map(function () { return qty; }) : [];
    line.label = qty > 0 ? 'Qty to order: ' + qty.toLocaleString() : 'Qty to order';
    chart.update('none');
  };

  // Load the chart into a detail row's chart pane the first time it is opened (or again after a failure)
  window.openHistory = function (pane) {
    if (pane._chart || pane._loading) { return; }
    var host = pane._host || pane.closest('[data-history-url]');
    if (!host) { return; }
    var sku = pane.getAttribute('data-sku');
    pane._loading = true;
    note(pane, 'Loading the last two years\u2026');
    Promise.all([fetchHistory(host.dataset.historyUrl, sku), loadChartLibrary(host.dataset.chartJs)])
      .then(function (results) {
        pane._loading = false;
        var row = pickRow(results[0], sku);
        // With no transactions at all the endpoint answers with a flat line and no description
        if (!row || !('raw_qty' in row)) { note(pane, 'No inventory transactions for ' + sku + ' in the last two years.'); return; }
        draw(pane, row, sku);
      })
      .catch(function (error) {
        pane._loading = false;
        var why = error && error.name === 'AbortError' ? 'it took too long' : (error && error.message) || 'unknown error';
        note(pane, 'Could not load the inventory history (' + why + '). The planner app has to be running on this server.', true);
      });
  };

  document.addEventListener('click', function (event) {
    var toggle = event.target.closest && event.target.closest('[data-history-raw]');
    if (!toggle) { return; }
    var pane = toggle.closest('[data-history-pane]');
    if (!pane || !pane._chart || !pane._series) { return; }
    var showingRaw = toggle.dataset.raw === 'true';
    pane._chart.data.datasets[0].data = showingRaw ? pane._series.values : pane._series.raw;
    toggle.dataset.raw = showingRaw ? 'false' : 'true';
    toggle.textContent = showingRaw ? 'Show raw data' : 'Show adjusted data';
    pane._chart.update();
  });
}());

// Purchase requisition: an A4 sheet in a pop-up. The report icon on a row loads it; Print and Download PDF
// both use the browser's print dialog (choose "Save as PDF" there), and only the sheet is printed.
(function () {
  var dialog = null;

  function build() {
    dialog = document.createElement('dialog');
    dialog.id = 'report-dialog';
    dialog.setAttribute('aria-label', 'Purchase requisition');
    dialog.innerHTML = '<div class="report-bar"><button type="button" class="btn btn-small" data-report-print>Print</button>' +
      '<button type="button" class="btn btn-outline btn-small" data-report-pdf>Download PDF</button>' +
      '<button type="button" class="btn btn-outline btn-small" data-report-close>Close</button></div>' +
      '<div class="report-body" data-report-body></div>';
    document.body.appendChild(dialog);
  }

  function updateWith(sheet) {
    if (window.setOrderLine) { window.setOrderLine(sheet.querySelector('[data-history-pane]')); }
    var cell = sheet.querySelector('[data-report-with]');
    if (!cell || sheet.dataset.missing) { return; }
    var qty = parseFloat(String(sheet.querySelector('[data-report-qty]').value).replace(/,/g, '')) || 0;
    var usage = parseFloat(sheet.dataset.usage), lead = parseFloat(sheet.dataset.lead), stock = parseFloat(sheet.dataset.stock);
    if (!usage) { return; }
    var months = (stock + qty - usage * lead) / usage;
    cell.textContent = (Math.round(months * 10) / 10).toFixed(1) + ' mo';
  }

  document.addEventListener('click', function (event) {
    var open = event.target.closest('[data-report-url]');
    if (open) {
      if (!dialog) { build(); }
      var row = open.closest('tr');
      var qtyBox = row && row.querySelector('[name="qty"]');
      var url = open.dataset.reportUrl + (location.search || '');
      var qty = qtyBox ? String(qtyBox.value).replace(/,/g, '') : '';
      url += (url.indexOf('?') === -1 ? '?' : '&') + 'qty=' + encodeURIComponent(qty);
      var body = dialog.querySelector('[data-report-body]');
      body.innerHTML = '<p class="muted report-loading">Loading…</p>';
      if (!dialog.open) { dialog.showModal(); }
      fetch(url, {credentials: 'same-origin'}).then(function (r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        return r.text();
      }).then(function (html) {
        body.innerHTML = html;
        var sheet = body.querySelector('.sheet');
        updateWith(sheet);
        var pane = body.querySelector('[data-history-pane]');
        if (pane) {
          pane.dataset.historyUrl = sheet.dataset.historyUrl;
          var host = document.querySelector('[data-chart-js]');
          pane._host = {dataset: {historyUrl: sheet.dataset.historyUrl, chartJs: host ? host.dataset.chartJs : ''}};
          if (window.openHistory) { window.openHistory(pane); }
        }
      }).catch(function (error) {
        body.innerHTML = '<p class="history-note is-error">Could not load the requisition (' + error.message + ').</p>';
      });
      return;
    }
    if (!dialog) { return; }
    if (event.target.closest('[data-report-close]')) { dialog.close(); }
    var print = event.target.closest('[data-report-print]');
    var pdf = event.target.closest('[data-report-pdf]');
    if (print || pdf) {
      var sheet = dialog.querySelector('.sheet');
      var oldTitle = document.title;
      if (pdf && sheet) {
        document.title = 'Purchase requisition ' + sheet.dataset.sku + ' ' + new Date().toISOString().slice(0, 10);
      }
      window.addEventListener('afterprint', function restore() {
        document.title = oldTitle;
        window.removeEventListener('afterprint', restore);
      });
      window.print();
    }
  });
  document.addEventListener('input', function (event) {
    if (event.target.matches && event.target.matches('[data-report-qty]')) { updateWith(event.target.closest('.sheet')); }
  });
}());
