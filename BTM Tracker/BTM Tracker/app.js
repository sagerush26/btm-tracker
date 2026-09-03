/* ===== BTM Competitor Tracker ===== */

/* -- Theme Toggle -- */
(function(){
  const t = document.querySelector('[data-theme-toggle]');
  const r = document.documentElement;
  let d = 'dark'; // default to dark for this dashboard
  r.setAttribute('data-theme', d);
  if (t) {
    t.addEventListener('click', function() {
      d = d === 'dark' ? 'light' : 'dark';
      r.setAttribute('data-theme', d);
      t.setAttribute('aria-label', 'Switch to ' + (d === 'dark' ? 'light' : 'dark') + ' mode');
      t.innerHTML = d === 'dark'
        ? '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
        : '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
      // Re-render charts with new colors
      renderCharts();
      if (trendsData) renderTrendCharts();
      if (modelMixData) renderModelMix(document.getElementById('mixDealerSelect').value);
    });
  }
})();

/* -- Data -- */
var competitorsData = null;
var scansData = null;
var trendsData = null;
var modelMixData = null;
var currentCondition = 'all'; // 'all', 'new', 'used'
var currentScanIndex = -1; // -1 means latest

var CHART_COLORS = [
  '#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#a855f7',
  '#06b6d4', '#f97316', '#ec4899', '#84cc16', '#14b8a6',
  '#6366f1', '#e11d48', '#0ea5e9', '#8b5cf6'
];

/* -- Load Data -- */
async function loadData() {
  try {
    var compResp = await fetch('./data/competitors.json');
    competitorsData = await compResp.json();

    var scanResp = await fetch('./data/scans.json');
    scansData = await scanResp.json();

    var trendResp = await fetch('./data/trends.json');
    trendsData = await trendResp.json();

    var mixResp = await fetch('./data/model-mix.json');
    modelMixData = await mixResp.json();

    populateScanSelector();
    renderDashboard();
  } catch (e) {
    console.error('Failed to load data:', e);
  }
}

/* -- Scan Selector -- */
function populateScanSelector() {
  var select = document.getElementById('scanDateSelect');
  if (!select || !scansData || !scansData.scans.length) return;

  select.innerHTML = '';
  // Latest first
  var scans = scansData.scans.slice().reverse();
  scans.forEach(function(scan, i) {
    var opt = document.createElement('option');
    var realIndex = scansData.scans.length - 1 - i;
    opt.value = realIndex;
    var d = new Date(scan.date + 'T00:00:00');
    var label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    if (i === 0) label += ' (Latest)';
    opt.textContent = label;
    select.appendChild(opt);
  });

  select.addEventListener('change', function() {
    currentScanIndex = parseInt(this.value);
    reRenderAll();
  });
}

function getSelectedScan() {
  if (!scansData || !scansData.scans.length) return null;
  if (currentScanIndex < 0 || currentScanIndex >= scansData.scans.length) {
    return scansData.scans[scansData.scans.length - 1];
  }
  return scansData.scans[currentScanIndex];
}

/* -- Navigation -- */
document.querySelectorAll('.nav-item[data-view]').forEach(function(item) {
  item.addEventListener('click', function(e) {
    e.preventDefault();
    var view = this.getAttribute('data-view');
    switchView(view);
    closeMobileMenu();
  });
});

/* -- Bottom Nav (Mobile) -- */
document.querySelectorAll('.bottom-nav-item[data-view]').forEach(function(item) {
  item.addEventListener('click', function(e) {
    e.preventDefault();
    var view = this.getAttribute('data-view');
    switchView(view);
  });
});

function switchView(view) {
  // Sync sidebar nav
  document.querySelectorAll('.nav-item').forEach(function(n) { n.classList.remove('active'); });
  var sidebarItem = document.querySelector('.nav-item[data-view="' + view + '"]');
  if (sidebarItem) sidebarItem.classList.add('active');

  // Sync bottom nav
  document.querySelectorAll('.bottom-nav-item').forEach(function(n) { n.classList.remove('active'); });
  var bottomItem = document.querySelector('.bottom-nav-item[data-view="' + view + '"]');
  if (bottomItem) bottomItem.classList.add('active');

  // Switch view
  document.querySelectorAll('.view-section').forEach(function(s) { s.classList.remove('active'); });
  document.getElementById('view-' + view).classList.add('active');

  var titles = {
    overview: 'Market Overview',
    inventory: 'Inventory Comparison',
    competitors: 'Competitor Profiles',
    modelmix: 'Model Mix',
    trends: 'Market Trends',
    changes: 'Change Log'
  };
  document.getElementById('pageTitle').textContent = titles[view] || 'Dashboard';

  // Scroll main content to top on view switch
  var mainEl = document.querySelector('.main');
  if (mainEl) mainEl.scrollTop = 0;
}

/* -- Mobile Menu -- */
function openMobileMenu() {
  var sidebar = document.querySelector('.sidebar');
  var overlay = document.getElementById('mobileOverlay');
  if (sidebar) sidebar.classList.add('open');
  if (overlay) {
    overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

function closeMobileMenu() {
  var sidebar = document.querySelector('.sidebar');
  var overlay = document.getElementById('mobileOverlay');
  if (sidebar) sidebar.classList.remove('open');
  if (overlay) {
    overlay.classList.remove('active');
    document.body.style.overflow = '';
  }
}

(function() {
  var menuBtn = document.getElementById('mobileMenuBtn');
  var overlay = document.getElementById('mobileOverlay');
  if (menuBtn) menuBtn.addEventListener('click', openMobileMenu);
  if (overlay) overlay.addEventListener('click', closeMobileMenu);
})();

/* -- Condition Filter Toggle -- */
document.querySelectorAll('.condition-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.condition-btn').forEach(function(b) { b.classList.remove('active'); });
    this.classList.add('active');
    currentCondition = this.getAttribute('data-condition');
    reRenderAll();
  });
});

function reRenderAll() {
  if (!scansData || !competitorsData) return;
  var scan = getSelectedScan();
  if (!scan) return;
  renderKPIs(scan);
  renderCharts();
  renderTable(scan);
  renderCompetitorCards(scan);
  if (modelMixData) renderModelMix(document.getElementById('mixDealerSelect').value);
}

/* -- Condition Filter Helpers -- */
function getFilteredBoatCount(result) {
  if (currentCondition === 'new') return result.newBoats;
  if (currentCondition === 'used') return result.usedBoats;
  return result.totalBoats;
}

function getFilteredAvgPrice(result) {
  if (currentCondition === 'new' && result.avgPriceNew) return result.avgPriceNew;
  if (currentCondition === 'used' && result.avgPriceUsed) return result.avgPriceUsed;
  return result.avgPrice;
}

function getConditionLabel() {
  if (currentCondition === 'new') return 'New';
  if (currentCondition === 'used') return 'Pre-Owned';
  return 'All';
}

/* -- Render Dashboard -- */
function renderDashboard() {
  var scan = getSelectedScan();
  if (!scan) return;

  updateScanStatus();
  renderKPIs(scan);
  renderCharts();
  renderTable(scan);
  renderCompetitorCards(scan);
  renderChanges();
  renderTrends();
  initModelMix();
  initDateRangeFilters();
  initCompareUI();
}

/* -- Scan Status -- */
function updateScanStatus() {
  var el = document.getElementById('scanStatus');
  // Use the latest scan date from scans.json (more reliable than competitors.json lastScan)
  var lastScanDate = competitorsData.lastScan;
  if (scansData && scansData.scans && scansData.scans.length > 0) {
    var lastScan = scansData.scans[scansData.scans.length - 1];
    lastScanDate = lastScan.date + 'T06:00:00Z'; // scans run at midnight CT = 6am UTC
  }
  var date = new Date(lastScanDate);
  var options = { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' };
  el.textContent = 'Last scan: ' + date.toLocaleDateString('en-US', options);
}

/* -- Helpers -- */
function isOwnDealer(competitorId) {
  var comp = competitorsData.competitors.find(function(c) { return c.id === competitorId; });
  return comp && comp.isOwn === true;
}

function getOwnResult(scan) {
  return scan.results.find(function(r) { return isOwnDealer(r.competitorId); });
}

function getCompetitorResults(scan) {
  return scan.results.filter(function(r) { return !isOwnDealer(r.competitorId); });
}

/* -- KPIs -- */
function renderKPIs(scan) {
  var grid = document.getElementById('kpiGrid');
  var own = getOwnResult(scan);
  var compResults = getCompetitorResults(scan);

  var ownCount = own ? getFilteredBoatCount(own) : 0;
  var competitorBoats = compResults.reduce(function(sum, r) { return sum + getFilteredBoatCount(r); }, 0);
  var dealerCount = compResults.length;

  var compAvgPrices = compResults.map(function(r) { return getFilteredAvgPrice(r); }).filter(function(p) { return p > 0; });
  var marketAvgPrice = compAvgPrices.length > 0 ? Math.round(compAvgPrices.reduce(function(a, b) { return a + b; }, 0) / compAvgPrices.length) : 0;

  var condLabel = getConditionLabel();
  var btmLabel = currentCondition === 'all' ? 'BTM Inventory' : 'BTM ' + condLabel;
  var compLabel = currentCondition === 'all' ? 'Competitor Inventory' : 'Competitor ' + condLabel;
  var priceLabel = currentCondition === 'all' ? 'Market Avg Price' : condLabel + ' Avg Price';

  var kpis = [
    { label: btmLabel, value: ownCount.toLocaleString(), delta: null, highlight: true },
    { label: 'Competitors Tracked', value: dealerCount, delta: null },
    { label: compLabel, value: competitorBoats.toLocaleString(), delta: null },
    { label: 'BTM New / Used', value: own ? own.newBoats + ' / ' + own.usedBoats : '—', delta: null, highlight: true },
    { label: priceLabel, value: marketAvgPrice > 0 ? '$' + marketAvgPrice.toLocaleString() : '—', delta: null }
  ];

  // When filtering by condition, replace new/used KPI with filtered avg price for BTM
  if (currentCondition !== 'all' && own) {
    var ownAvg = getFilteredAvgPrice(own);
    kpis[3] = { label: 'BTM ' + condLabel + ' Avg', value: ownAvg > 0 ? '$' + ownAvg.toLocaleString() : '—', delta: null, highlight: true };
  }

  grid.innerHTML = kpis.map(function(k) {
    var deltaHtml = '';
    if (k.delta) {
      var cls = k.delta > 0 ? 'up' : (k.delta < 0 ? 'down' : 'flat');
      var arrow = k.delta > 0 ? '↑' : (k.delta < 0 ? '↓' : '→');
      deltaHtml = '<span class="kpi-delta ' + cls + '">' + arrow + ' ' + Math.abs(k.delta) + '% vs prev scan</span>';
    }
    var cardClass = k.highlight ? 'kpi-card own-highlight' : 'kpi-card';
    return '<div class="' + cardClass + '">' +
      '<span class="kpi-label">' + k.label + '</span>' +
      '<span class="kpi-value">' + k.value + '</span>' +
      deltaHtml +
    '</div>';
  }).join('');
}

/* -- Charts -- */
var chartInstances = {};

function getChartTextColor() {
  var raw = getComputedStyle(document.documentElement).getPropertyValue('--color-text-muted').trim();
  return raw || '#94a3b8';
}

function getChartGridColor() {
  var raw = getComputedStyle(document.documentElement).getPropertyValue('--color-divider').trim();
  return raw || '#334155';
}

function setChartDefaults() {
  var textColor = getChartTextColor();
  Chart.defaults.color = textColor;
}

function renderCharts() {
  var latestScan = getSelectedScan();
  if (!latestScan) return;

  setChartDefaults();

  var sorted = latestScan.results.slice().sort(function(a, b) { return getFilteredBoatCount(b) - getFilteredBoatCount(a); });

  var names = sorted.map(function(r) {
    var comp = competitorsData.competitors.find(function(c) { return c.id === r.competitorId; });
    return comp ? comp.name : r.competitorId;
  });

  var shortNames = names.map(function(n) {
    if (n.length > 18) return n.substring(0, 16) + '…';
    return n;
  });

  var textColor = getChartTextColor();
  var gridColor = getChartGridColor();

  // Inventory Bar Chart
  if (chartInstances.inventory) chartInstances.inventory.destroy();
  chartInstances.inventory = new Chart(document.getElementById('chartInventory'), {
    type: 'bar',
    data: {
      labels: shortNames,
      datasets: [{
        label: getConditionLabel() + ' Boats',
        data: sorted.map(function(r) { return getFilteredBoatCount(r); }),
        backgroundColor: sorted.map(function(_, i) { return CHART_COLORS[i % CHART_COLORS.length]; }),
        borderRadius: 4,
        borderSkipped: false
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: function(items) { return names[items[0].dataIndex]; }
          }
        }
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } }
        },
        y: {
          grid: { display: false },
          ticks: { color: textColor, font: { size: 11 } }
        }
      }
    }
  });

  // New vs Used Stacked Bar
  if (chartInstances.condition) chartInstances.condition.destroy();
  var condDatasets = [];
  if (currentCondition === 'all' || currentCondition === 'new') {
    condDatasets.push({
      label: 'New',
      data: sorted.map(function(r) { return r.newBoats; }),
      backgroundColor: '#3b82f6',
      borderRadius: 2
    });
  }
  if (currentCondition === 'all' || currentCondition === 'used') {
    condDatasets.push({
      label: 'Pre-Owned',
      data: sorted.map(function(r) { return r.usedBoats; }),
      backgroundColor: '#f59e0b',
      borderRadius: 2
    });
  }
  chartInstances.condition = new Chart(document.getElementById('chartCondition'), {
    type: 'bar',
    data: {
      labels: shortNames,
      datasets: condDatasets
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: {
        legend: {
          position: 'top',
          labels: { color: textColor, font: { size: 11 }, boxWidth: 12, padding: 16 }
        },
        tooltip: {
          callbacks: {
            title: function(items) { return names[items[0].dataIndex]; }
          }
        }
      },
      scales: {
        x: {
          stacked: true,
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } }
        },
        y: {
          stacked: true,
          grid: { display: false },
          ticks: { color: textColor, font: { size: 11 } }
        }
      }
    }
  });

  // Market Share Doughnut
  if (chartInstances.marketShare) chartInstances.marketShare.destroy();
  chartInstances.marketShare = new Chart(document.getElementById('chartMarketShare'), {
    type: 'doughnut',
    data: {
      labels: names,
      datasets: [{
        data: sorted.map(function(r) { return getFilteredBoatCount(r); }),
        backgroundColor: sorted.map(function(_, i) { return CHART_COLORS[i % CHART_COLORS.length]; }),
        borderColor: 'transparent',
        borderWidth: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '55%',
      plugins: {
        legend: {
          position: 'right',
          labels: {
            color: textColor,
            font: { size: 11 },
            boxWidth: 12,
            padding: 8,
            generateLabels: function(chart) {
              var data = chart.data;
              var total = data.datasets[0].data.reduce(function(a, b) { return a + b; }, 0);
              return data.labels.map(function(label, i) {
                var val = data.datasets[0].data[i];
                var pct = ((val / total) * 100).toFixed(1);
                return {
                  text: label + ' (' + pct + '%)',
                  fillStyle: data.datasets[0].backgroundColor[i],
                  fontColor: textColor,
                  hidden: false,
                  index: i
                };
              });
            }
          }
        },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              var total = ctx.dataset.data.reduce(function(a, b) { return a + b; }, 0);
              var pct = ((ctx.raw / total) * 100).toFixed(1);
              return ctx.label + ': ' + ctx.raw + ' boats (' + pct + '%)';
            }
          }
        }
      }
    }
  });
}

/* -- Table -- */
var currentSort = { key: 'total', dir: 'desc' };

function renderTable(scan) {
  var results = scan.results.slice();
  sortResults(results);

  var tbody = document.getElementById('tableBody');
  tbody.innerHTML = results.map(function(r, i) {
    var comp = competitorsData.competitors.find(function(c) { return c.id === r.competitorId; });
    if (!comp) return '';
    var color = CHART_COLORS[competitorsData.competitors.indexOf(comp) % CHART_COLORS.length];
    var own = comp.isOwn === true;
    var rowClass = own ? 'own-row' : '';
    var ownBadge = own ? '<span class="own-badge">You</span>' : '';

    var filteredTotal = getFilteredBoatCount(r);
    var filteredAvg = getFilteredAvgPrice(r);

    return '<tr class="' + rowClass + '">' +
      '<td><div class="dealer-name-cell"><span class="dealer-dot" style="background:' + color + '"></span><a class="dealer-link" href="' + comp.url + '" target="_blank" rel="noopener noreferrer">' + comp.name + '</a>' + ownBadge + '</div></td>' +
      '<td><strong>' + filteredTotal + '</strong></td>' +
      '<td><span class="badge badge-new">' + r.newBoats + '</span></td>' +
      '<td><span class="badge badge-used">' + r.usedBoats + '</span></td>' +
      '<td>$' + filteredAvg.toLocaleString() + '</td>' +
      '<td>' + r.priceRange + '</td>' +
      '<td style="white-space:normal;max-width:200px;font-size:var(--text-xs);color:var(--color-text-muted)">' + comp.brands.join(', ') + '</td>' +
      '<td style="font-size:var(--text-xs);color:var(--color-text-muted)">' + comp.location + '</td>' +
    '</tr>';
  }).join('');
}

function sortResults(results) {
  var key = currentSort.key;
  var dir = currentSort.dir === 'asc' ? 1 : -1;

  results.sort(function(a, b) {
    var aComp = competitorsData.competitors.find(function(c) { return c.id === a.competitorId; });
    var bComp = competitorsData.competitors.find(function(c) { return c.id === b.competitorId; });

    if (key === 'name') {
      var aName = aComp ? aComp.name : '';
      var bName = bComp ? bComp.name : '';
      return dir * aName.localeCompare(bName);
    }
    if (key === 'total') return dir * (getFilteredBoatCount(a) - getFilteredBoatCount(b));
    if (key === 'new') return dir * (a.newBoats - b.newBoats);
    if (key === 'used') return dir * (a.usedBoats - b.usedBoats);
    if (key === 'avg') return dir * (getFilteredAvgPrice(a) - getFilteredAvgPrice(b));
    return 0;
  });
}

document.querySelectorAll('th[data-sort]').forEach(function(th) {
  th.addEventListener('click', function() {
    var key = this.getAttribute('data-sort');
    if (currentSort.key === key) {
      currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
    } else {
      currentSort = { key: key, dir: 'desc' };
    }

    document.querySelectorAll('th').forEach(function(h) { h.classList.remove('sorted'); });
    this.classList.add('sorted');

    var scan = getSelectedScan();
    renderTable(scan);
  });
});

/* -- Competitor Cards -- */
function renderCompetitorCards(scan) {
  var grid = document.getElementById('competitorGrid');
  var sorted = scan.results.slice().sort(function(a, b) { return getFilteredBoatCount(b) - getFilteredBoatCount(a); });

  grid.innerHTML = sorted.map(function(r) {
    var comp = competitorsData.competitors.find(function(c) { return c.id === r.competitorId; });
    if (!comp) return '';
    var color = CHART_COLORS[competitorsData.competitors.indexOf(comp) % CHART_COLORS.length];

    var own = comp.isOwn === true;
    var cardClass = own ? 'competitor-card own-card' : 'competitor-card';
    var ownBadge = own ? '<span class="own-badge">You</span>' : '';

    var filteredTotal = getFilteredBoatCount(r);
    var filteredAvg = getFilteredAvgPrice(r);
    var condLabel = getConditionLabel();

    var statsHtml;
    if (currentCondition === 'all') {
      statsHtml =
        '<div class="stat-mini"><span class="stat-mini-value">' + r.totalBoats + '</span><span class="stat-mini-label">Total</span></div>' +
        '<div class="stat-mini"><span class="stat-mini-value">' + r.newBoats + '</span><span class="stat-mini-label">New</span></div>' +
        '<div class="stat-mini"><span class="stat-mini-value">' + r.usedBoats + '</span><span class="stat-mini-label">Used</span></div>' +
        '<div class="stat-mini"><span class="stat-mini-value">$' + (r.avgPrice / 1000).toFixed(0) + 'k</span><span class="stat-mini-label">Avg Price</span></div>';
    } else {
      statsHtml =
        '<div class="stat-mini"><span class="stat-mini-value">' + filteredTotal + '</span><span class="stat-mini-label">' + condLabel + '</span></div>' +
        '<div class="stat-mini"><span class="stat-mini-value">$' + (filteredAvg / 1000).toFixed(0) + 'k</span><span class="stat-mini-label">' + condLabel + ' Avg</span></div>' +
        '<div class="stat-mini"><span class="stat-mini-value">' + r.totalBoats + '</span><span class="stat-mini-label">All Boats</span></div>';
    }

    return '<div class="' + cardClass + '">' +
      '<div style="display:flex;align-items:center;gap:var(--space-3)">' +
        '<span class="dealer-dot" style="background:' + color + ';width:12px;height:12px"></span>' +
        '<span class="competitor-card-name">' + comp.name + '</span>' + ownBadge +
      '</div>' +
      '<div class="competitor-card-brands">' + comp.brands.join(' · ') + '</div>' +
      '<div class="competitor-card-stats">' + statsHtml + '</div>' +
      '<a class="competitor-card-link" href="' + comp.url + '" target="_blank" rel="noopener noreferrer">View inventory →</a>' +
    '</div>';
  }).join('');
}

/* -- Changes -- */
function renderChanges() {
  var section = document.getElementById('changesSection');
  var changes = scansData.changes || [];

  if (changes.length === 0) {
    section.innerHTML = '<div class="empty-state">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/></svg>' +
      '<div class="empty-state-title">No Changes Detected Yet</div>' +
      '<div class="empty-state-desc">Changes will appear here after the next scan compares inventory against today\'s baseline. Scans run nightly at midnight.</div>' +
    '</div>';
    return;
  }

  // Group changes by date
  var grouped = {};
  changes.forEach(function(c) {
    if (!grouped[c.date]) grouped[c.date] = [];
    grouped[c.date].push(c);
  });

  var dates = Object.keys(grouped).sort().reverse();

  section.innerHTML = dates.map(function(date) {
    var items = grouped[date];
    var increaseCount = items.filter(function(i) { return i.type === 'inventory_increase'; }).length;
    var decreaseCount = items.filter(function(i) { return i.type === 'inventory_decrease'; }).length;
    var priceCount = items.filter(function(i) { return i.type === 'price_change'; }).length;

    var totalAdded = 0, totalRemoved = 0;
    items.forEach(function(i) {
      if (i.type === 'inventory_increase') totalAdded += (i.count || 0);
      if (i.type === 'inventory_decrease') totalRemoved += (i.count || 0);
    });

    var badgesHtml = '';
    if (totalAdded > 0) badgesHtml += '<span class="change-badge added">+' + totalAdded + ' Added</span> ';
    if (totalRemoved > 0) badgesHtml += '<span class="change-badge removed">-' + totalRemoved + ' Likely Sold</span> ';
    if (priceCount > 0) badgesHtml += '<span class="change-badge price-change">' + priceCount + ' Price Changes</span>';

    var itemsHtml = items.map(function(item) {
      var isDecrease = item.type === 'inventory_decrease';
      var isIncrease = item.type === 'inventory_increase';
      var iconClass = isDecrease ? 'removed' : (isIncrease ? 'added' : 'price');
      var iconText = isDecrease ? '\u2212' : (isIncrease ? '+' : '$');
      var dealerName = item.dealerName || item.dealer || 'Unknown Dealer';

      return '<div class="change-item">' +
        '<div class="change-icon ' + iconClass + '">' + iconText + '</div>' +
        '<div class="change-details">' +
          '<div class="change-boat">' + dealerName + '</div>' +
          '<div class="change-meta">' + (item.detail || '') + '</div>' +
        '</div>' +
      '</div>';
    }).join('');

    var d = new Date(date + 'T00:00:00');
    var dateFormatted = d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });

    return '<div class="change-card">' +
      '<div class="change-header">' +
        '<div class="change-date">' + dateFormatted + '</div>' +
        '<div>' + badgesHtml + '</div>' +
      '</div>' +
      itemsHtml +
    '</div>';
  }).join('');
}

/* -- Model Mix -- */
function initModelMix() {
  if (!modelMixData) return;

  // Populate dealer dropdown
  var select = document.getElementById('mixDealerSelect');
  // Clear existing options beyond first two
  while (select.options.length > 2) select.remove(2);

  modelMixData.dealers.forEach(function(d) {
    if (d.isOwn) return; // BTM already has its own option
    var opt = document.createElement('option');
    opt.value = d.id;
    opt.textContent = d.name;
    select.appendChild(opt);
  });

  select.addEventListener('change', function() {
    renderModelMix(this.value);
  });

  renderModelMix('market');
}

function getConditionMixFields(source) {
  // Select the right sub-fields based on condition filter
  // Market totals use totalNew/totalUsed, dealers use newBoats/usedBoats
  if (currentCondition === 'new') {
    var newTotal = source.newBoats || source.totalNew || (source.byCondition && source.byCondition.New) || source.totalBoats || source.totalBoatsAnalyzed;
    return {
      byCategory: source.byCategoryNew || source.byCategory,
      byLength: source.byLengthNew || source.byLength,
      byPropulsion: source.byPropulsionNew || source.byPropulsion,
      byBrand: source.byBrandNew || source.byBrand,
      total: newTotal
    };
  }
  if (currentCondition === 'used') {
    var usedTotal = source.usedBoats || source.totalUsed || (source.byCondition && source.byCondition.Used) || source.totalBoats || source.totalBoatsAnalyzed;
    return {
      byCategory: source.byCategoryUsed || source.byCategory,
      byLength: source.byLengthUsed || source.byLength,
      byPropulsion: source.byPropulsionUsed || source.byPropulsion,
      byBrand: source.byBrandUsed || source.byBrand,
      total: usedTotal
    };
  }
  return {
    byCategory: source.byCategory,
    byLength: source.byLength,
    byPropulsion: source.byPropulsion,
    byBrand: source.byBrand,
    total: source.totalBoats || source.totalBoatsAnalyzed || source.detailedCount
  };
}

function getMixData(filterValue) {
  var source;
  var label;
  if (filterValue === 'market') {
    source = modelMixData.marketTotals;
    label = 'Entire Market';
  } else if (filterValue === 'btm') {
    source = modelMixData.dealers.find(function(d) { return d.isOwn; });
    label = 'Big Thunder Marine';
  } else {
    source = modelMixData.dealers.find(function(d) { return d.id === filterValue; });
    label = source ? source.name : '';
  }
  if (!source) return null;

  var fields = getConditionMixFields(source);
  var condLabel = getConditionLabel();
  if (currentCondition !== 'all') label += ' (' + condLabel + ')';

  return {
    label: label,
    byCategory: fields.byCategory,
    byLength: fields.byLength,
    byPropulsion: fields.byPropulsion,
    byCondition: source.byCondition,
    byBrand: fields.byBrand,
    total: fields.total
  };
}

function renderModelMix(filterValue) {
  setChartDefaults();
  var textColor = getChartTextColor();
  var gridColor = getChartGridColor();
  var data = getMixData(filterValue);
  if (!data) return;

  var mixCountLabel = currentCondition !== 'all' ? data.total + ' ' + getConditionLabel().toLowerCase() + ' boats analyzed' : data.total + ' boats analyzed';
  document.getElementById('mixBoatCount').textContent = mixCountLabel;

  // Category bar
  var catLabels = Object.keys(data.byCategory);
  var catValues = Object.values(data.byCategory);
  if (chartInstances.mixCategory) chartInstances.mixCategory.destroy();
  chartInstances.mixCategory = new Chart(document.getElementById('mixCategory'), {
    type: 'bar',
    data: {
      labels: catLabels,
      datasets: [{ data: catValues, backgroundColor: catLabels.map(function(_, i) { return CHART_COLORS[i % CHART_COLORS.length]; }), borderRadius: 4, borderSkipped: false }]
    },
    options: { responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 } }, beginAtZero: true }, y: { grid: { display: false }, ticks: { color: textColor, font: { size: 11 } } } } }
  });

  // Category pie
  if (chartInstances.mixCategoryPie) chartInstances.mixCategoryPie.destroy();
  chartInstances.mixCategoryPie = new Chart(document.getElementById('mixCategoryPie'), {
    type: 'doughnut',
    data: {
      labels: catLabels,
      datasets: [{ data: catValues, backgroundColor: catLabels.map(function(_, i) { return CHART_COLORS[i % CHART_COLORS.length]; }), borderColor: 'transparent', borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '55%',
      plugins: {
        legend: {
          position: 'right',
          labels: {
            color: textColor, font: { size: 11 }, boxWidth: 12, padding: 8,
            generateLabels: function(chart) {
              var d = chart.data;
              var total = d.datasets[0].data.reduce(function(a, b) { return a + b; }, 0);
              return d.labels.map(function(label, i) {
                var val = d.datasets[0].data[i];
                var pct = total > 0 ? ((val / total) * 100).toFixed(1) : 0;
                return { text: label + ' (' + pct + '%)', fillStyle: d.datasets[0].backgroundColor[i], fontColor: textColor, hidden: false, index: i };
              });
            }
          }
        }
      }
    }
  });

  // Length bar
  var lengthOrder = ['Under 20ft', '20-25ft', '25-30ft', '30-35ft', '35-40ft', '40ft+', 'Unknown'];
  var lengthLabels = lengthOrder.filter(function(l) { return data.byLength[l] !== undefined; });
  var lengthValues = lengthLabels.map(function(l) { return data.byLength[l] || 0; });
  var lengthColors = ['#06b6d4', '#3b82f6', '#8b5cf6', '#f59e0b', '#ef4444', '#ec4899', '#64748b'];
  if (chartInstances.mixLength) chartInstances.mixLength.destroy();
  chartInstances.mixLength = new Chart(document.getElementById('mixLength'), {
    type: 'bar',
    data: {
      labels: lengthLabels,
      datasets: [{ data: lengthValues, backgroundColor: lengthLabels.map(function(_, i) { return lengthColors[i % lengthColors.length]; }), borderRadius: 4, borderSkipped: false }]
    },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { color: textColor, font: { size: 11 } } }, y: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 } }, beginAtZero: true } } }
  });

  // Propulsion doughnut
  var propLabels = Object.keys(data.byPropulsion).filter(function(k) { return k !== 'Unknown' || data.byPropulsion[k] > 0; });
  var propValues = propLabels.map(function(k) { return data.byPropulsion[k]; });
  var propColors = ['#3b82f6', '#22c55e', '#f59e0b', '#a855f7', '#64748b'];
  if (chartInstances.mixPropulsion) chartInstances.mixPropulsion.destroy();
  chartInstances.mixPropulsion = new Chart(document.getElementById('mixPropulsion'), {
    type: 'doughnut',
    data: {
      labels: propLabels,
      datasets: [{ data: propValues, backgroundColor: propLabels.map(function(_, i) { return propColors[i % propColors.length]; }), borderColor: 'transparent', borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '55%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: textColor, font: { size: 12 }, boxWidth: 14, padding: 16,
            generateLabels: function(chart) {
              var d = chart.data;
              var total = d.datasets[0].data.reduce(function(a, b) { return a + b; }, 0);
              return d.labels.map(function(label, i) {
                var val = d.datasets[0].data[i];
                var pct = total > 0 ? ((val / total) * 100).toFixed(0) : 0;
                return { text: label + ' (' + pct + '%)', fillStyle: d.datasets[0].backgroundColor[i], fontColor: textColor, hidden: false, index: i };
              });
            }
          }
        }
      }
    }
  });

  // Condition doughnut
  var condLabels = Object.keys(data.byCondition);
  var condValues = Object.values(data.byCondition);
  if (chartInstances.mixCondition) chartInstances.mixCondition.destroy();
  chartInstances.mixCondition = new Chart(document.getElementById('mixCondition'), {
    type: 'doughnut',
    data: {
      labels: condLabels,
      datasets: [{ data: condValues, backgroundColor: ['#3b82f6', '#f59e0b', '#64748b'], borderColor: 'transparent', borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '55%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: textColor, font: { size: 12 }, boxWidth: 14, padding: 16,
            generateLabels: function(chart) {
              var d = chart.data;
              var total = d.datasets[0].data.reduce(function(a, b) { return a + b; }, 0);
              return d.labels.map(function(label, i) {
                var val = d.datasets[0].data[i];
                var pct = total > 0 ? ((val / total) * 100).toFixed(0) : 0;
                return { text: label + ' (' + pct + '%)', fillStyle: d.datasets[0].backgroundColor[i], fontColor: textColor, hidden: false, index: i };
              });
            }
          }
        }
      }
    }
  });

  // Top brands bar
  var brandEntries = Object.entries(data.byBrand).slice(0, 12);
  var brandLabels = brandEntries.map(function(e) { return e[0]; });
  var brandValues = brandEntries.map(function(e) { return e[1]; });
  if (chartInstances.mixBrand) chartInstances.mixBrand.destroy();
  chartInstances.mixBrand = new Chart(document.getElementById('mixBrand'), {
    type: 'bar',
    data: {
      labels: brandLabels,
      datasets: [{ data: brandValues, backgroundColor: brandLabels.map(function(_, i) { return CHART_COLORS[i % CHART_COLORS.length]; }), borderRadius: 4, borderSkipped: false }]
    },
    options: { responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 } }, beginAtZero: true }, y: { grid: { display: false }, ticks: { color: textColor, font: { size: 11 } } } } }
  });

  // BTM vs Market comparison (always shown)
  renderMixComparison(textColor, gridColor);
}

function renderMixComparison(textColor, gridColor) {
  var btmRaw = modelMixData.dealers.find(function(d) { return d.isOwn; });
  if (!btmRaw) return;

  var btmFields = getConditionMixFields(btmRaw);
  var marketFields = getConditionMixFields(modelMixData.marketTotals);
  var btmTotal = btmFields.total || 1;
  var marketTotal = marketFields.total || 1;

  // Get all categories across both
  var allCats = Object.keys(marketFields.byCategory);
  var btmPcts = allCats.map(function(c) { return (((btmFields.byCategory[c] || 0) / btmTotal) * 100).toFixed(1); });
  var marketPcts = allCats.map(function(c) { return (((marketFields.byCategory[c] || 0) / marketTotal) * 100).toFixed(1); });

  var compCondLabel = currentCondition !== 'all' ? ' (' + getConditionLabel() + ')' : '';
  if (chartInstances.mixComparison) chartInstances.mixComparison.destroy();
  chartInstances.mixComparison = new Chart(document.getElementById('mixComparison'), {
    type: 'bar',
    data: {
      labels: allCats,
      datasets: [
        { label: 'BTM' + compCondLabel, data: btmPcts, backgroundColor: '#22c55e', borderRadius: 4, borderSkipped: false },
        { label: 'Market' + compCondLabel, data: marketPcts, backgroundColor: '#3b82f6', borderRadius: 4, borderSkipped: false }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { color: textColor, font: { size: 11 }, boxWidth: 14, padding: 16 } },
        tooltip: { callbacks: { label: function(ctx) { return ctx.dataset.label + ': ' + ctx.raw + '%'; } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: textColor, font: { size: 11 }, maxRotation: 45 } },
        y: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 }, callback: function(v) { return v + '%'; } }, beginAtZero: true }
      }
    }
  });

  // Propulsion comparison
  var allProps = Object.keys(marketFields.byPropulsion).filter(function(k) { return k !== 'Unknown'; });
  var btmPropPcts = allProps.map(function(p) { return (((btmFields.byPropulsion[p] || 0) / btmTotal) * 100).toFixed(1); });
  var marketPropPcts = allProps.map(function(p) { return (((marketFields.byPropulsion[p] || 0) / marketTotal) * 100).toFixed(1); });

  if (chartInstances.mixPropComparison) chartInstances.mixPropComparison.destroy();
  chartInstances.mixPropComparison = new Chart(document.getElementById('mixPropComparison'), {
    type: 'bar',
    data: {
      labels: allProps,
      datasets: [
        { label: 'BTM' + compCondLabel, data: btmPropPcts, backgroundColor: '#22c55e', borderRadius: 4, borderSkipped: false },
        { label: 'Market' + compCondLabel, data: marketPropPcts, backgroundColor: '#3b82f6', borderRadius: 4, borderSkipped: false }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { color: textColor, font: { size: 11 }, boxWidth: 14, padding: 16 } },
        tooltip: { callbacks: { label: function(ctx) { return ctx.dataset.label + ': ' + ctx.raw + '%'; } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: textColor, font: { size: 11 } } },
        y: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 }, callback: function(v) { return v + '%'; } }, beginAtZero: true }
      }
    }
  });
}

/* -- Trends -- */
var TREND_COLORS = {
  category: ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#a855f7', '#06b6d4', '#f97316', '#ec4899', '#84cc16', '#14b8a6', '#6366f1'],
  priceBand: ['#22c55e', '#3b82f6', '#f59e0b', '#ef4444', '#a855f7', '#ec4899'],
  lengthBand: ['#06b6d4', '#3b82f6', '#f59e0b', '#ef4444', '#a855f7', '#ec4899']
};

function renderTrends() {
  if (!trendsData) return;

  renderTrendKPIs();
  renderTrendCharts();
  renderRecentActivity();
  renderInsightsTimeline();
  updateInsightsBanner();
}

function renderTrendKPIs() {
  var grid = document.getElementById('trendKpiGrid');
  var s = trendsData.summary;

  // Compute totals from changes data
  var changes = scansData.changes || [];
  var totalSold = 0, totalAdded = 0;
  var soldByCat = {};
  changes.forEach(function(c) {
    if (c.type === 'inventory_decrease') totalSold += (c.count || 0);
    if (c.type === 'inventory_increase') totalAdded += (c.count || 0);
  });

  // Compute hottest category from soldByDealer in trends
  var soldByDealer = trendsData.soldByDealer || {};
  var topDealer = Object.keys(soldByDealer).sort(function(a, b) {
    return (soldByDealer[b] || 0) - (soldByDealer[a] || 0);
  })[0] || 'Awaiting data';

  // Find hottest category from soldByCategory or model-mix changes
  var hottestCat = 'Awaiting data';
  var sbc = trendsData.soldByCategory || {};
  var sbcKeys = Object.keys(sbc);
  if (sbcKeys.length > 0) {
    hottestCat = sbcKeys.sort(function(a, b) { return (sbc[b] || 0) - (sbc[a] || 0); })[0];
  }

  var kpis = [
    { label: 'Total Sold', value: totalSold.toLocaleString(), icon: '\u2193' },
    { label: 'Total Added', value: totalAdded.toLocaleString(), icon: '\u2191' },
    { label: 'Most Active Dealer', value: topDealer, icon: '\u2B50' },
    { label: 'Hottest Category', value: hottestCat, icon: '\uD83D\uDD25' },
    { label: 'Dealers Tracked', value: (scansData.scans.length > 0 ? scansData.scans[scansData.scans.length - 1].results.length : 0), icon: '\uD83C\uDFEA' }
  ];

  grid.innerHTML = kpis.map(function(k) {
    return '<div class="kpi-card">' +
      '<span class="kpi-label">' + k.label + '</span>' +
      '<span class="kpi-value">' + k.value + '</span>' +
    '</div>';
  }).join('');
}

function renderTrendCharts() {
  setChartDefaults();
  var textColor = getChartTextColor();
  var gridColor = getChartGridColor();

  // Compute sold-by breakdowns from changes + model-mix data
  var soldByCat = {}, soldByDealer = {};
  var changes = scansData.changes || [];
  changes.forEach(function(c) {
    if (c.type === 'inventory_decrease') {
      var name = c.dealerName || c.dealer || 'Unknown';
      soldByDealer[name] = (soldByDealer[name] || 0) + (c.count || 0);
    }
  });

  // Use soldByDealer from trends data (cumulative)
  var trendSoldByDealer = trendsData.soldByDealer || {};

  // Sold by Category -- horizontal bar
  renderBarChart('chartSoldCategory', trendsData.soldByCategory || {}, TREND_COLORS.category, textColor, gridColor);

  // Sold by Price Band -- horizontal bar
  renderBarChart('chartSoldPrice', trendsData.soldByPriceBand || {}, TREND_COLORS.priceBand, textColor, gridColor);

  // Sold by Length -- horizontal bar
  renderBarChart('chartSoldLength', trendsData.soldByLengthBand || {}, TREND_COLORS.lengthBand, textColor, gridColor);

  // Sold by Condition -- doughnut
  renderConditionDoughnut(textColor);

  // Inventory Trend Line
  renderInventoryTrendLine(textColor, gridColor);

  // Price Trend Line
  renderPriceTrendLine(textColor, gridColor);

  // Top Sold Brands -- horizontal bar
  renderBrandBar(textColor, gridColor);

  // Sales by Dealer -- horizontal bar
  renderDealerBar(textColor, gridColor);
}

function renderBarChart(canvasId, dataObj, colors, textColor, gridColor) {
  var labels = Object.keys(dataObj);
  var values = Object.values(dataObj);
  var hasData = values.some(function(v) { return v > 0; });

  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  chartInstances[canvasId] = new Chart(document.getElementById(canvasId), {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: labels.map(function(_, i) { return colors[i % colors.length]; }),
        borderRadius: 4,
        borderSkipped: false
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: {
        legend: { display: false },
        title: hasData ? undefined : {
          display: true,
          text: 'Data builds after scans detect changes',
          color: textColor,
          font: { size: 12, style: 'italic' },
          padding: { top: 60 }
        }
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 }, stepSize: 1 },
          beginAtZero: true
        },
        y: {
          grid: { display: false },
          ticks: { color: textColor, font: { size: 11 } }
        }
      }
    }
  });
}

function renderConditionDoughnut(textColor) {
  var data = trendsData.soldByCondition;
  var labels = Object.keys(data);
  var values = Object.values(data);
  var hasData = values.some(function(v) { return v > 0; });

  if (chartInstances.soldCondition) chartInstances.soldCondition.destroy();
  chartInstances.soldCondition = new Chart(document.getElementById('chartSoldCondition'), {
    type: 'doughnut',
    data: {
      labels: labels,
      datasets: [{
        data: hasData ? values : [1],
        backgroundColor: hasData ? ['#3b82f6', '#f59e0b'] : ['#334155'],
        borderColor: 'transparent',
        borderWidth: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '60%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: textColor,
            font: { size: 12 },
            boxWidth: 14,
            padding: 16,
            generateLabels: function(chart) {
              if (!hasData) {
                return [{ text: 'Awaiting data', fillStyle: '#334155', fontColor: textColor, hidden: false, index: 0 }];
              }
              var d = chart.data;
              var total = d.datasets[0].data.reduce(function(a, b) { return a + b; }, 0);
              return d.labels.map(function(label, i) {
                var val = d.datasets[0].data[i];
                var pct = total > 0 ? ((val / total) * 100).toFixed(0) : 0;
                return { text: label + ' (' + pct + '%)', fillStyle: d.datasets[0].backgroundColor[i], fontColor: textColor, hidden: false, index: i };
              });
            }
          }
        }
      }
    }
  });
}

function renderInventoryTrendLine(textColor, gridColor) {
  var trend = trendsData.inventoryTrend;
  var labels = trend.map(function(t) {
    var d = new Date(t.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });

  if (chartInstances.inventoryTrend) chartInstances.inventoryTrend.destroy();
  chartInstances.inventoryTrend = new Chart(document.getElementById('chartInventoryTrend'), {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Total Market',
          data: trend.map(function(t) { return t.totalMarket; }),
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59, 130, 246, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: trend.length < 20 ? 4 : 2,
          pointBackgroundColor: '#3b82f6',
          borderWidth: 2
        },
        {
          label: 'Big Thunder Marine',
          data: trend.map(function(t) { return t.btm; }),
          borderColor: '#22c55e',
          backgroundColor: 'rgba(34, 197, 94, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: trend.length < 20 ? 4 : 2,
          pointBackgroundColor: '#22c55e',
          borderWidth: 2
        },
        {
          label: 'Competitors',
          data: trend.map(function(t) { return t.competitors; }),
          borderColor: '#f59e0b',
          backgroundColor: 'transparent',
          fill: false,
          tension: 0.3,
          pointRadius: trend.length < 20 ? 3 : 1,
          pointBackgroundColor: '#f59e0b',
          borderWidth: 1.5,
          borderDash: [5, 3]
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { color: textColor, font: { size: 11 }, boxWidth: 14, padding: 16 }
        }
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } }
        },
        y: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } },
          beginAtZero: false
        }
      }
    }
  });
}

function renderPriceTrendLine(textColor, gridColor) {
  var trend = trendsData.priceTrend;
  var labels = trend.map(function(t) {
    var d = new Date(t.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });

  if (chartInstances.priceTrend) chartInstances.priceTrend.destroy();
  chartInstances.priceTrend = new Chart(document.getElementById('chartPriceTrend'), {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Market Average',
          data: trend.map(function(t) { return t.marketAvg; }),
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59, 130, 246, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: trend.length < 20 ? 4 : 2,
          pointBackgroundColor: '#3b82f6',
          borderWidth: 2
        },
        {
          label: 'BTM Average',
          data: trend.map(function(t) { return t.btmAvg; }),
          borderColor: '#22c55e',
          backgroundColor: 'rgba(34, 197, 94, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: trend.length < 20 ? 4 : 2,
          pointBackgroundColor: '#22c55e',
          borderWidth: 2
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { color: textColor, font: { size: 11 }, boxWidth: 14, padding: 16 }
        },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              return ctx.dataset.label + ': $' + ctx.raw.toLocaleString();
            }
          }
        }
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } }
        },
        y: {
          grid: { color: gridColor },
          ticks: {
            color: textColor,
            font: { size: 11 },
            callback: function(v) { return '$' + (v / 1000).toFixed(0) + 'k'; }
          },
          beginAtZero: false
        }
      }
    }
  });
}

function renderBrandBar(textColor, gridColor) {
  var brandData = trendsData.soldByBrand || {};
  var entries = Object.entries(brandData).sort(function(a, b) { return b[1] - a[1]; }).slice(0, 10);
  var labels = entries.map(function(e) { return e[0]; });
  var values = entries.map(function(e) { return e[1]; });
  var hasData = values.length > 0 && values.some(function(v) { return v > 0; });

  if (chartInstances.soldBrand) chartInstances.soldBrand.destroy();

  if (!hasData) {
    chartInstances.soldBrand = new Chart(document.getElementById('chartSoldBrand'), {
      type: 'bar',
      data: { labels: ['Awaiting data'], datasets: [{ data: [0], backgroundColor: '#334155' }] },
      options: {
        responsive: true, maintainAspectRatio: false, indexAxis: 'y',
        plugins: {
          legend: { display: false },
          title: { display: true, text: 'Data builds after scans detect changes', color: textColor, font: { size: 12, style: 'italic' }, padding: { top: 60 } }
        },
        scales: {
          x: { grid: { color: gridColor }, ticks: { color: textColor }, beginAtZero: true },
          y: { grid: { display: false }, ticks: { color: textColor } }
        }
      }
    });
    return;
  }

  chartInstances.soldBrand = new Chart(document.getElementById('chartSoldBrand'), {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{ data: values, backgroundColor: labels.map(function(_, i) { return CHART_COLORS[i % CHART_COLORS.length]; }), borderRadius: 4, borderSkipped: false }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, indexAxis: 'y',
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 }, stepSize: 1 }, beginAtZero: true },
        y: { grid: { display: false }, ticks: { color: textColor, font: { size: 11 } } }
      }
    }
  });
}

function renderDealerBar(textColor, gridColor) {
  var dealerData = trendsData.soldByDealer || {};
  var entries = Object.entries(dealerData).sort(function(a, b) { return b[1] - a[1]; }).slice(0, 10);
  var labels = entries.map(function(e) { return e[0].length > 20 ? e[0].substring(0, 18) + '…' : e[0]; });
  var values = entries.map(function(e) { return e[1]; });
  var hasData = values.length > 0 && values.some(function(v) { return v > 0; });

  if (chartInstances.soldDealer) chartInstances.soldDealer.destroy();

  if (!hasData) {
    chartInstances.soldDealer = new Chart(document.getElementById('chartSoldDealer'), {
      type: 'bar',
      data: { labels: ['Awaiting data'], datasets: [{ data: [0], backgroundColor: '#334155' }] },
      options: {
        responsive: true, maintainAspectRatio: false, indexAxis: 'y',
        plugins: {
          legend: { display: false },
          title: { display: true, text: 'Data builds after scans detect changes', color: textColor, font: { size: 12, style: 'italic' }, padding: { top: 60 } }
        },
        scales: {
          x: { grid: { color: gridColor }, ticks: { color: textColor }, beginAtZero: true },
          y: { grid: { display: false }, ticks: { color: textColor } }
        }
      }
    });
    return;
  }

  chartInstances.soldDealer = new Chart(document.getElementById('chartSoldDealer'), {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{ data: values, backgroundColor: labels.map(function(_, i) { return CHART_COLORS[i % CHART_COLORS.length]; }), borderRadius: 4, borderSkipped: false }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, indexAxis: 'y',
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 }, stepSize: 1 }, beginAtZero: true },
        y: { grid: { display: false }, ticks: { color: textColor, font: { size: 11 } } }
      }
    }
  });
}

function renderRecentActivity() {
  // Recently Sold table (dealer-level inventory decreases)
  var soldContainer = document.getElementById('recentlySoldTable');
  var soldItems = trendsData.recentlySold || [];

  if (soldItems.length === 0) {
    soldContainer.innerHTML = '<div class="empty-state-mini">No sales detected yet -- data builds after future scans.</div>';
  } else {
    var soldHtml = '<table class="trend-table"><thead><tr><th>Dealer</th><th>Boats Sold</th><th>Details</th><th>Date</th></tr></thead><tbody>';
    soldItems.slice().reverse().slice(0, 30).forEach(function(item) {
      soldHtml += '<tr>' +
        '<td>' + (item.dealer || item.dealerName || '') + '</td>' +
        '<td>' + (item.count || 1) + '</td>' +
        '<td>' + (item.detail || '') + '</td>' +
        '<td>' + (item.date || '') + '</td>' +
      '</tr>';
    });
    soldHtml += '</tbody></table>';
    soldContainer.innerHTML = soldHtml;
  }

  // Recently Added table (dealer-level inventory increases)
  var addedContainer = document.getElementById('recentlyAddedTable');
  var addedItems = trendsData.recentlyAdded || [];

  if (addedItems.length === 0) {
    addedContainer.innerHTML = '<div class="empty-state-mini">No new listings detected yet -- data builds after future scans.</div>';
  } else {
    var addedHtml = '<table class="trend-table"><thead><tr><th>Dealer</th><th>Boats Added</th><th>Details</th><th>Date</th></tr></thead><tbody>';
    addedItems.slice().reverse().slice(0, 30).forEach(function(item) {
      addedHtml += '<tr>' +
        '<td>' + (item.dealer || item.dealerName || '') + '</td>' +
        '<td>' + (item.count || 1) + '</td>' +
        '<td>' + (item.detail || '') + '</td>' +
        '<td>' + (item.date || '') + '</td>' +
      '</tr>';
    });
    addedHtml += '</tbody></table>';
    addedContainer.innerHTML = addedHtml;
  }
}

function renderInsightsTimeline() {
  var container = document.getElementById('insightsTimeline');
  var insights = trendsData.insights || [];

  if (insights.length === 0) {
    container.innerHTML = '<div class="empty-state-mini">Insights will appear here as trend data accumulates.</div>';
    return;
  }

  // Show newest first
  var sorted = insights.slice().reverse();

  container.innerHTML = sorted.map(function(ins) {
    var priority = ins.priority || 'info';
    var tagLabel = priority === 'hot' ? 'Hot' : (priority === 'warning' ? 'Alert' : (priority === 'success' ? 'Good' : 'Info'));

    var d = new Date(ins.date + 'T00:00:00');
    var dateStr = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

    return '<div class="insight-item ' + priority + '">' +
      '<div class="insight-date">' + dateStr + '</div>' +
      '<div class="insight-text">' +
        '<span class="insight-tag ' + priority + '">' + tagLabel + '</span>' +
        ins.text +
      '</div>' +
    '</div>';
  }).join('');
}

function updateInsightsBanner() {
  var banner = document.getElementById('insightsBannerText');

  // Compute totals from changes data
  var changes = scansData.changes || [];
  var totalSold = 0, totalAdded = 0;
  changes.forEach(function(c) {
    if (c.type === 'inventory_decrease') totalSold += (c.count || 0);
    if (c.type === 'inventory_increase') totalAdded += (c.count || 0);
  });

  if (totalSold === 0 && totalAdded === 0) {
    banner.textContent = 'Trend data will build over time as scans detect inventory changes.';
    return;
  }

  var parts = [];
  if (totalSold > 0) parts.push(totalSold + ' boats likely sold');
  if (totalAdded > 0) parts.push(totalAdded + ' boats added');

  var soldByDealer = trendsData.soldByDealer || {};
  var topDealer = Object.keys(soldByDealer).sort(function(a, b) {
    return (soldByDealer[b] || 0) - (soldByDealer[a] || 0);
  })[0];
  if (topDealer) parts.push('most active: ' + topDealer);

  banner.textContent = 'Since tracking began: ' + parts.join(' · ') + '.';
}

/* -- Date Range Filtering -- */
function initDateRangeFilters() {
  // Trends date range
  var applyBtn = document.getElementById('applyDateRange');
  var resetBtn = document.getElementById('resetDateRange');
  if (applyBtn) {
    applyBtn.addEventListener('click', function() {
      var startVal = document.getElementById('trendStartDate').value;
      var endVal = document.getElementById('trendEndDate').value;
      renderTrendsFiltered(startVal, endVal);
    });
  }
  if (resetBtn) {
    resetBtn.addEventListener('click', function() {
      document.getElementById('trendStartDate').value = '';
      document.getElementById('trendEndDate').value = '';
      renderTrends();
    });
  }

  // Changes date range
  var applyChanges = document.getElementById('applyChangesDateRange');
  var resetChanges = document.getElementById('resetChangesDateRange');
  if (applyChanges) {
    applyChanges.addEventListener('click', function() {
      var startVal = document.getElementById('changesStartDate').value;
      var endVal = document.getElementById('changesEndDate').value;
      renderChangesFiltered(startVal, endVal);
    });
  }
  if (resetChanges) {
    resetChanges.addEventListener('click', function() {
      document.getElementById('changesStartDate').value = '';
      document.getElementById('changesEndDate').value = '';
      renderChanges();
    });
  }
}

function renderTrendsFiltered(startDate, endDate) {
  if (!trendsData) return;

  // Filter inventory trend
  var filteredInv = trendsData.inventoryTrend;
  var filteredPrice = trendsData.priceTrend;

  if (startDate) {
    filteredInv = filteredInv.filter(function(t) { return t.date >= startDate; });
    filteredPrice = filteredPrice.filter(function(t) { return t.date >= startDate; });
  }
  if (endDate) {
    filteredInv = filteredInv.filter(function(t) { return t.date <= endDate; });
    filteredPrice = filteredPrice.filter(function(t) { return t.date <= endDate; });
  }

  // Re-render with filtered data
  setChartDefaults();
  var textColor = getChartTextColor();
  var gridColor = getChartGridColor();

  // Filtered inventory trend
  renderFilteredTrendLine('chartInventoryTrend', 'inventoryTrend', filteredInv, textColor, gridColor);
  renderFilteredPriceLine('chartPriceTrend', 'priceTrend', filteredPrice, textColor, gridColor);

  // Filter changes within date range for sold/added counts
  var filteredChanges = (scansData.changes || []).filter(function(c) {
    if (startDate && c.date < startDate) return false;
    if (endDate && c.date > endDate) return false;
    return true;
  });

  // Recalculate sold-by breakdowns from filtered changes
  var soldByCat = {}, soldByPrice = {}, soldByLen = {}, soldByCond = {}, soldByBrand = {}, soldByDealer = {};
  var addedByCat = {};
  var recentSold = [], recentAdded = [];

  filteredChanges.forEach(function(c) {
    if (c.type === 'inventory_decrease') {
      var name = c.dealerName || c.dealer || 'Unknown';
      soldByDealer[name] = (soldByDealer[name] || 0) + (c.count || 0);
      recentSold.push(c);
    } else if (c.type === 'inventory_increase') {
      var name2 = c.dealerName || c.dealer || 'Unknown';
      recentAdded.push(c);
    }
  });

  // Re-render category/price/length bar charts with filtered data
  renderBarChart('chartSoldCategory', soldByCat, TREND_COLORS.category, textColor, gridColor);
  renderBarChart('chartSoldPrice', soldByPrice, TREND_COLORS.priceBand, textColor, gridColor);
  renderBarChart('chartSoldLength', soldByLen, TREND_COLORS.lengthBand, textColor, gridColor);

  // Update KPIs for filtered range
  var trendGrid = document.getElementById('trendKpiGrid');
  var totalSold = 0, totalAdded2 = 0;
  recentSold.forEach(function(c) { totalSold += (c.count || 0); });
  recentAdded.forEach(function(c) { totalAdded2 += (c.count || 0); });
  var topFilterDealer = Object.keys(soldByDealer).sort(function(a,b){ return (soldByDealer[b]||0)-(soldByDealer[a]||0); })[0] || 'N/A';

  trendGrid.innerHTML = [
    { label: 'Sold (filtered)', value: totalSold },
    { label: 'Added (filtered)', value: totalAdded2 },
    { label: 'Most Active Dealer', value: topFilterDealer },
  ].map(function(k) {
    return '<div class="kpi-card"><span class="kpi-label">' + k.label + '</span><span class="kpi-value">' + k.value + '</span></div>';
  }).join('');
}

function renderFilteredTrendLine(canvasId, chartKey, data, textColor, gridColor) {
  var labels = data.map(function(t) {
    var d = new Date(t.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });

  if (chartInstances[chartKey]) chartInstances[chartKey].destroy();
  chartInstances[chartKey] = new Chart(document.getElementById(canvasId), {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Total Market',
          data: data.map(function(t) { return t.totalMarket; }),
          borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)',
          fill: true, tension: 0.3, pointRadius: data.length < 20 ? 4 : 2,
          pointBackgroundColor: '#3b82f6', borderWidth: 2
        },
        {
          label: 'Big Thunder Marine',
          data: data.map(function(t) { return t.btm; }),
          borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)',
          fill: true, tension: 0.3, pointRadius: data.length < 20 ? 4 : 2,
          pointBackgroundColor: '#22c55e', borderWidth: 2
        },
        {
          label: 'Competitors',
          data: data.map(function(t) { return t.competitors; }),
          borderColor: '#f59e0b', backgroundColor: 'transparent',
          fill: false, tension: 0.3, pointRadius: data.length < 20 ? 3 : 1,
          pointBackgroundColor: '#f59e0b', borderWidth: 1.5, borderDash: [5, 3]
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { color: textColor, font: { size: 11 }, boxWidth: 14, padding: 16 } } },
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 } } },
        y: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 } }, beginAtZero: false }
      }
    }
  });
}

function renderFilteredPriceLine(canvasId, chartKey, data, textColor, gridColor) {
  var labels = data.map(function(t) {
    var d = new Date(t.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });

  if (chartInstances[chartKey]) chartInstances[chartKey].destroy();
  chartInstances[chartKey] = new Chart(document.getElementById(canvasId), {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Market Average', data: data.map(function(t) { return t.marketAvg; }),
          borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)',
          fill: true, tension: 0.3, pointRadius: data.length < 20 ? 4 : 2,
          pointBackgroundColor: '#3b82f6', borderWidth: 2
        },
        {
          label: 'BTM Average', data: data.map(function(t) { return t.btmAvg; }),
          borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)',
          fill: true, tension: 0.3, pointRadius: data.length < 20 ? 4 : 2,
          pointBackgroundColor: '#22c55e', borderWidth: 2
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { color: textColor, font: { size: 11 }, boxWidth: 14, padding: 16 } },
        tooltip: { callbacks: { label: function(ctx) { return ctx.dataset.label + ': $' + ctx.raw.toLocaleString(); } } }
      },
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 } } },
        y: { grid: { color: gridColor }, ticks: { color: textColor, font: { size: 11 }, callback: function(v) { return '$' + (v / 1000).toFixed(0) + 'k'; } }, beginAtZero: false }
      }
    }
  });
}

function renderChangesFiltered(startDate, endDate) {
  var section = document.getElementById('changesSection');
  var changes = (scansData.changes || []).filter(function(c) {
    if (startDate && c.date < startDate) return false;
    if (endDate && c.date > endDate) return false;
    return true;
  });

  if (changes.length === 0) {
    var rangeText = '';
    if (startDate || endDate) {
      rangeText = 'No changes found in the selected date range.';
    } else {
      rangeText = 'Changes will appear here after the next scan compares inventory against the baseline. Scans run nightly at midnight.';
    }
    section.innerHTML = '<div class="empty-state">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/></svg>' +
      '<div class="empty-state-title">No Changes Found</div>' +
      '<div class="empty-state-desc">' + rangeText + '</div>' +
    '</div>';
    return;
  }

  // Group changes by date (reuse existing renderChanges logic)
  var grouped = {};
  changes.forEach(function(c) {
    if (!grouped[c.date]) grouped[c.date] = [];
    grouped[c.date].push(c);
  });

  var dates = Object.keys(grouped).sort().reverse();

  section.innerHTML = dates.map(function(date) {
    var items = grouped[date];
    var addCount = items.filter(function(i) { return i.type === 'added'; }).length;
    var removeCount = items.filter(function(i) { return i.type === 'removed'; }).length;
    var priceCount = items.filter(function(i) { return i.type === 'price_change'; }).length;

    var badgesHtml = '';
    if (addCount > 0) badgesHtml += '<span class="change-badge added">+' + addCount + ' Added</span> ';
    if (removeCount > 0) badgesHtml += '<span class="change-badge removed">-' + removeCount + ' Sold/Removed</span> ';
    if (priceCount > 0) badgesHtml += '<span class="change-badge price-change">' + priceCount + ' Price Changes</span>';

    var itemsHtml = items.map(function(item) {
      var iconClass = item.type === 'added' ? 'added' : (item.type === 'removed' ? 'removed' : 'price');
      var iconText = item.type === 'added' ? '+' : (item.type === 'removed' ? '−' : '$');
      return '<div class="change-item"><div class="change-icon ' + iconClass + '">' + iconText + '</div><div class="change-details"><div class="change-boat">' + item.boat + '</div><div class="change-meta">' + item.dealer + ' · ' + (item.detail || '') + '</div></div></div>';
    }).join('');

    var d = new Date(date + 'T00:00:00');
    var dateFormatted = d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
    return '<div class="change-card"><div class="change-header"><div class="change-date">' + dateFormatted + '</div><div>' + badgesHtml + '</div></div>' + itemsHtml + '</div>';
  }).join('');
}

/* -- Comparison Mode -- */
function initCompareUI() {
  var compareBtn = document.getElementById('compareBtn');
  var comparePanel = document.getElementById('comparePanel');
  var compareClose = document.getElementById('compareClose');
  var runCompare = document.getElementById('runCompare');

  if (!compareBtn || !scansData) return;

  // Populate compare selectors
  var selA = document.getElementById('compareScanA');
  var selB = document.getElementById('compareScanB');
  if (!selA || !selB) return;

  selA.innerHTML = '';
  selB.innerHTML = '';

  scansData.scans.slice().reverse().forEach(function(scan, i) {
    var realIdx = scansData.scans.length - 1 - i;
    var d = new Date(scan.date + 'T00:00:00');
    var label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    if (i === 0) label += ' (Latest)';

    var optA = document.createElement('option');
    optA.value = realIdx;
    optA.textContent = label;
    selA.appendChild(optA);

    var optB = document.createElement('option');
    optB.value = realIdx;
    optB.textContent = label;
    selB.appendChild(optB);
  });

  // Default: if 2+ scans, set A to second-latest, B to latest
  if (scansData.scans.length >= 2) {
    selA.value = scansData.scans.length - 2;
    selB.value = scansData.scans.length - 1;
  }

  compareBtn.addEventListener('click', function() {
    var isVisible = comparePanel.style.display !== 'none';
    comparePanel.style.display = isVisible ? 'none' : 'block';
    compareBtn.classList.toggle('active', !isVisible);
  });

  compareClose.addEventListener('click', function() {
    comparePanel.style.display = 'none';
    compareBtn.classList.remove('active');
  });

  runCompare.addEventListener('click', function() {
    var idxA = parseInt(selA.value);
    var idxB = parseInt(selB.value);
    renderCompareResults(idxA, idxB);
  });
}

function renderCompareResults(idxA, idxB) {
  var container = document.getElementById('compareResults');
  if (!container) return;

  var scanA = scansData.scans[idxA];
  var scanB = scansData.scans[idxB];
  if (!scanA || !scanB) {
    container.innerHTML = '<div class="empty-state-mini">Select two different scans to compare.</div>';
    return;
  }

  var dateA = new Date(scanA.date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  var dateB = new Date(scanB.date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

  // Build comparison table
  var html = '<table class="compare-table">' +
    '<thead><tr><th>Dealer</th><th>' + dateA + '</th><th>' + dateB + '</th><th>Change</th></tr></thead><tbody>';

  // Match by competitorId
  var allIds = [];
  scanA.results.forEach(function(r) { if (allIds.indexOf(r.competitorId) === -1) allIds.push(r.competitorId); });
  scanB.results.forEach(function(r) { if (allIds.indexOf(r.competitorId) === -1) allIds.push(r.competitorId); });

  var totalA = 0, totalB = 0;

  allIds.forEach(function(id) {
    var comp = competitorsData.competitors.find(function(c) { return c.id === id; });
    var name = comp ? comp.name : id;
    var rA = scanA.results.find(function(r) { return r.competitorId === id; });
    var rB = scanB.results.find(function(r) { return r.competitorId === id; });
    var vA = rA ? getFilteredBoatCount(rA) : 0;
    var vB = rB ? getFilteredBoatCount(rB) : 0;
    var delta = vB - vA;
    totalA += vA;
    totalB += vB;

    var deltaClass = delta > 0 ? 'positive' : (delta < 0 ? 'negative' : 'neutral');
    var deltaStr = delta > 0 ? '+' + delta : (delta < 0 ? '' + delta : '—');

    html += '<tr>' +
      '<td><strong>' + name + '</strong></td>' +
      '<td>' + vA + '</td>' +
      '<td>' + vB + '</td>' +
      '<td><span class="compare-delta ' + deltaClass + '">' + deltaStr + '</span></td>' +
    '</tr>';
  });

  // Totals row
  var totalDelta = totalB - totalA;
  var totalClass = totalDelta > 0 ? 'positive' : (totalDelta < 0 ? 'negative' : 'neutral');
  var totalStr = totalDelta > 0 ? '+' + totalDelta : (totalDelta < 0 ? '' + totalDelta : '—');
  html += '<tr style="font-weight:700;border-top:2px solid var(--color-divider)"><td>Market Total</td><td>' + totalA + '</td><td>' + totalB + '</td><td><span class="compare-delta ' + totalClass + '">' + totalStr + '</span></td></tr>';

  // Average price comparison
  html += '</tbody></table>';
  html += '<h4 style="margin-top:var(--space-4);margin-bottom:var(--space-2);font-size:var(--text-sm);color:var(--color-text)">Average Price Comparison</h4>';
  html += '<table class="compare-table"><thead><tr><th>Dealer</th><th>' + dateA + '</th><th>' + dateB + '</th><th>Change</th></tr></thead><tbody>';

  allIds.forEach(function(id) {
    var comp = competitorsData.competitors.find(function(c) { return c.id === id; });
    var name = comp ? comp.name : id;
    var rA = scanA.results.find(function(r) { return r.competitorId === id; });
    var rB = scanB.results.find(function(r) { return r.competitorId === id; });
    var pA = rA ? getFilteredAvgPrice(rA) : 0;
    var pB = rB ? getFilteredAvgPrice(rB) : 0;
    var delta = pB - pA;
    var deltaClass = delta > 0 ? 'positive' : (delta < 0 ? 'negative' : 'neutral');
    var deltaStr = delta !== 0 ? (delta > 0 ? '+' : '') + '$' + Math.abs(delta).toLocaleString() : '—';

    html += '<tr><td><strong>' + name + '</strong></td><td>$' + pA.toLocaleString() + '</td><td>$' + pB.toLocaleString() + '</td><td><span class="compare-delta ' + deltaClass + '">' + deltaStr + '</span></td></tr>';
  });

  html += '</tbody></table>';
  container.innerHTML = html;
}

/* -- Init -- */
loadData();