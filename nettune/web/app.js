'use strict';

// All dynamic text is inserted with textContent (never innerHTML): Wi-Fi names
// come from the air and must be treated as untrusted input.

const TOKEN = document.querySelector('meta[name="nettune-token"]').content;
const $ = (id) => document.getElementById(id);
const state = { admin: false, conn: null, results: {}, analysis: null, running: false, fixes: [], actions: [] };

// ---------------------------------------------------------------- helpers

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style') Object.assign(node.style, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (typeof value === 'boolean') node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function fill(target, ...kids) {
  const node = typeof target === 'string' ? $(target) : target;
  node.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  node.hidden = false;
  return node;
}

async function api(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: { 'X-NetTune-Token': TOKEN, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    const error = new Error('The NetTune engine is not responding. Check that its window is still open.');
    error.code = 'engine_down';
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (data.code === 'token' && !sessionStorage.getItem('nt-reloaded')) {
      sessionStorage.setItem('nt-reloaded', '1');
      location.reload();
    }
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.code = data.code;
    throw error;
  }
  sessionStorage.removeItem('nt-reloaded');
  return data;
}

const SYMBOL = { good: '✓', warn: '!', bad: '×', info: 'i', off: '–' };
const dot = (kind) => el('span', { class: `dot ${kind}`, 'aria-hidden': 'true', text: SYMBOL[kind] || '' });
const pill = (kind, label) => el('span', { class: 'pill' }, dot(kind), label);

function toast(message, kind = 'info') {
  const node = el('div', { class: 'toast', role: 'status' }, dot(kind), el('span', { text: message }));
  $('toasts').append(node);
  setTimeout(() => node.remove(), kind === 'bad' ? 9000 : 5000);
}

function overlay(text) {
  $('overlay-text').textContent = text || '';
  $('overlay').hidden = !text;
}

const num = (value, digits = 1) => (value == null ? null : Number(value).toFixed(digits).replace(/\.0+$/, ''));
const mbps = (bps) => num((bps || 0) / 1e6, 2);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- tabs

const loaders = { fixes: () => loadFixes(), combine: () => loadCombine(), profiles: () => loadProfiles(), history: () => loadHistory() };

function showTab(name) {
  for (const button of document.querySelectorAll('.tabs button')) {
    button.setAttribute('aria-selected', String(button.dataset.tab === name));
  }
  for (const section of document.querySelectorAll('.tab')) section.hidden = section.id !== `tab-${name}`;
  clearInterval(combine.timer);
  if (loaders[name]) loaders[name]();
}

// ---------------------------------------------------------------- status + connection

async function loadStatus() {
  try {
    const status = await api('GET', '/api/status');
    state.admin = status.admin;
    state.conn = status.connection;
    state.results.connection = status.connection;
    $('version').textContent = `v${status.version}`;
    $('notice').replaceChildren();
    renderRights();
    renderConnection();
    if (status.connection.hotspot_likely) $('opt-mode').value = 'lite';
  } catch (err) {
    fill('notice', el('div', { class: 'notice' }, el('span', { text: err.message })));
  }
}

function renderRights() {
  if (state.admin) {
    fill('rights', pill('good', 'Administrator'));
  } else {
    fill('rights', pill('off', 'Standard user'),
      el('button', { class: 'btn', onclick: elevate, title: 'Some fixes change system settings and need administrator rights' }, 'Restart as administrator'));
  }
}

function fact(label, value, extra) {
  return el('div', {}, el('div', { class: 'fact-label', text: label }), el('div', { class: 'fact-value' }, value ?? '—'), extra);
}

function renderConnection() {
  const conn = state.conn;
  if (!conn || !conn.connected) {
    fill('conn', el('div', { class: 'conn-head' }, el('span', { class: 'conn-name', text: 'Not connected' })),
      el('p', { class: 'muted', text: 'Connect to Wi-Fi or a hotspot, then reload this page.' }));
    return;
  }
  const wifi = conn.wifi && !conn.wifi.error ? conn.wifi : null;
  const pills = [];
  if (wifi) {
    if (wifi.generation) pills.push(pill('info', wifi.generation));
    if (wifi.band) pills.push(pill(wifi.band === '2.4' ? 'warn' : 'good', `${wifi.band} GHz`));
  } else {
    pills.push(pill('info', conn.is_wifi ? 'Wi-Fi' : 'Wired / tethered'));
  }
  if (conn.hotspot_likely) pills.push(pill('info', 'Looks like a phone hotspot'));

  const rate = (wifi && wifi.rx_mbps) || conn.link_mbps;
  const facts = [];
  if (wifi && wifi.signal != null) {
    facts.push(fact('Signal', `${wifi.signal}%`, el('div', { class: 'meter' }, el('i', { style: { width: `${wifi.signal}%` } }))));
  }
  facts.push(fact('Link rate', rate != null ? `${num(rate)} Mbps` : null));
  if (wifi && wifi.channel != null) facts.push(fact('Channel', String(wifi.channel)));
  facts.push(fact('Adapter', conn.description));
  facts.push(fact('Driver', [conn.driver_version, conn.driver_date].filter(Boolean).join(' · ') || null));
  facts.push(fact('Address', conn.ip));
  facts.push(fact('Router', conn.gateway));
  facts.push(fact('DNS', (conn.dns || []).join(', ') || null));

  fill('conn',
    el('div', { class: 'conn-head' }, el('span', { class: 'conn-name', text: conn.label || 'Network' }), pills),
    el('div', { class: 'facts' }, facts),
    conn.wifi && conn.wifi.error ? el('div', { class: 'hint', text: conn.wifi.message }) : null);
}

async function elevate() {
  if (!confirm('NetTune will restart with administrator rights. Windows will ask for your permission.')) return;
  overlay('Waiting for Windows permission…');
  try {
    await api('POST', '/api/elevate');
  } catch (err) {
    overlay(null);
    toast(err.message, 'bad');
    return;
  }
  overlay('Restarting NetTune as administrator…');
  for (let attempt = 0; attempt < 60; attempt++) {
    await sleep(700);
    try {
      const ping = await fetch('/api/ping').then((r) => r.json());
      if (ping.admin) { location.reload(); return; }
    } catch (err) { /* engine is restarting */ }
  }
  overlay(null);
  toast('The engine did not come back. Start NetTune again from start.bat.', 'bad');
}

// ---------------------------------------------------------------- diagnosis

const STEPS = [
  { id: 'connection', label: 'Connection' },
  { id: 'latency', label: 'Router and internet latency' },
  { id: 'dns', label: 'DNS speed' },
  { id: 'nearby', label: 'Nearby networks' },
  { id: 'background', label: 'Background usage' },
  { id: 'speed', label: 'Speed test' },
  { id: 'analyze', label: 'Analysis' },
];

function setStep(id, status) {
  const item = document.querySelector(`#steps li[data-step="${id}"]`);
  if (!item) return;
  item.className = status;
  const icon = { active: el('span', { class: 'spinner' }), done: dot('good'), failed: dot('bad'), pending: dot('off') }[status];
  item.replaceChildren(icon, item.dataset.label);
}

async function runStep(id, fn) {
  setStep(id, 'active');
  try {
    await fn();
    setStep(id, 'done');
    return true;
  } catch (err) {
    setStep(id, 'failed');
    toast(`${STEPS.find((s) => s.id === id).label}: ${err.message}`, 'bad');
    return false;
  }
}

async function runDiagnosis() {
  if (state.running) return;
  state.running = true;
  const button = $('run');
  button.disabled = true;
  button.textContent = 'Running…';
  const withSpeed = $('opt-speed').checked;
  const steps = STEPS.filter((s) => withSpeed || s.id !== 'speed');
  fill('steps', steps.map((s) => el('li', { 'data-step': s.id, 'data-label': s.label })));
  steps.forEach((s) => setStep(s.id, 'pending'));
  state.results = {};
  state.analysis = null;
  $('verdict').replaceChildren();
  $('findings').replaceChildren();

  try {
    const online = await runStep('connection', async () => {
      await loadStatus();
      if (!state.conn || !state.conn.connected) throw new Error('No active network connection.');
    });
    if (!online) return;

    // Low-traffic probes can share the line; throughput tests run alone.
    await Promise.all([
      runStep('latency', async () => { state.results.latency = await api('POST', '/api/test/latency'); renderLatency(); renderTiles(); }),
      runStep('dns', async () => { state.results.dns = await api('POST', '/api/test/dns'); renderDns(); renderTiles(); }),
      runStep('nearby', async () => { state.results.nearby = await api('POST', '/api/test/nearby'); renderNearby(); }),
    ]);
    await runStep('background', async () => { state.results.background = await api('POST', '/api/test/background'); renderBackground(); });
    if (withSpeed) {
      await runStep('speed', async () => {
        const poll = setInterval(async () => {
          try { renderTiles(await api('GET', '/api/progress')); } catch (err) { /* keep polling */ }
        }, 450);
        try {
          state.results.speed = await api('POST', '/api/test/speed', { mode: $('opt-mode').value });
        } finally {
          clearInterval(poll);
        }
        renderTiles();
        for (const message of state.results.speed.errors || []) toast(message, 'warn');
      });
    }
    await runStep('analyze', () => analyze(true));
  } finally {
    state.running = false;
    button.disabled = false;
    button.textContent = 'Run again';
  }
}

async function analyze(save) {
  await loadFixes(true);
  state.analysis = await api('POST', '/api/analyze', { save });
  renderAnalysis();
}

function tile(label, value, unit, note) {
  return el('div', { class: 'tile' },
    el('div', { class: 'tile-label', text: label }),
    el('div', { class: 'tile-value' }, value ?? '—', value != null && unit ? el('span', { class: 'tile-unit', text: unit }) : null),
    el('div', { class: 'tile-note', text: note || ' ' }));
}

function renderTiles(progress) {
  const { latency, dns, speed } = state.results;
  const tiles = [];
  if ($('opt-speed').checked || speed) {
    const live = progress && progress.phase !== 'idle' ? progress : null;
    const down = speed ? num(speed.download_mbps, 1) : live && live.phase === 'download' ? num(live.mbps, 1) : null;
    const up = speed ? num(speed.upload_mbps, 1) : live && live.phase === 'upload' ? num(live.mbps, 1) : null;
    const used = speed ? `${num((speed.bytes_down + speed.bytes_up) / 1e6, 0)} MB used · ${speed.source || 'no server'}` : null;
    tiles.push(tile('Download', down, 'Mbps', speed ? used : live ? `measuring ${live.phase}…` : 'not tested yet'));
    tiles.push(tile('Upload', up, 'Mbps', speed ? (speed.mode === 'lite' ? 'light test' : 'full test') : null));
  }
  if (latency) {
    const net = latency.internet || {};
    const gw = latency.gateway;
    tiles.push(tile('Internet ping', num(net.avg_ms), 'ms', net.received ? `to ${net.name}${net.method === 'tcp' ? ' (TCP handshake)' : ''}` : 'no reply'));
    tiles.push(tile('Jitter', num(net.jitter_ms), 'ms', 'variation between pings'));
    tiles.push(tile('Packet loss', net.received ? num(net.loss_pct) : null, '%', 'on the internet path'));
    tiles.push(tile('Router ping', gw && gw.received ? num(gw.avg_ms) : null, 'ms', gw ? (gw.received ? `${num(gw.loss_pct)}% loss on the Wi-Fi hop` : 'router ignores pings') : 'no router address'));
  }
  if (dns && dns.current) tiles.push(tile('DNS lookup', num(dns.current.median_ms), 'ms', dns.current.reachable ? 'current DNS, median' : 'current DNS not answering'));
  fill('tiles', tiles);
}

function renderAnalysis() {
  const analysis = state.analysis;
  if (!analysis) return;
  fill('verdict', el('div', { class: 'card verdict' }, el('div', { class: 'verdict-title', text: analysis.headline })));
  const rows = analysis.findings.map((finding) => {
    const buttons = [];
    if (finding.fix) {
      const meta = state.fixes.find((f) => f.id === finding.fix);
      buttons.push(el('button', {
        class: 'btn primary',
        onclick: async (event) => {
          event.target.disabled = true;
          const changed = await changeFix(finding.fix, 'apply', finding.fix_params || {});
          if (changed) await analyze(false).catch(() => {});
          else event.target.disabled = false;
        },
      }, meta && meta.requires_admin && !state.admin ? 'Fix (needs admin)' : 'Apply fix'));
    }
    if (finding.action) {
      const meta = state.actions.find((a) => a.id === finding.action);
      buttons.push(el('button', { class: 'btn', onclick: () => runAction(finding.action) }, finding.action === 'reconnect_wifi' ? 'Reconnect now' : (meta ? meta.title : 'Run')));
    }
    return el('div', { class: 'finding' },
      dot(finding.severity),
      el('div', { class: 'finding-body' },
        el('div', { class: 'finding-title', text: finding.title }),
        el('div', { class: 'finding-detail', text: finding.detail })),
      buttons.length ? el('div', { class: 'finding-actions' }, buttons) : null);
  });
  fill('findings', el('div', { class: 'card' }, el('h2', { text: 'What we found' }), rows));
}

function renderDns() {
  const dns = state.results.dns;
  const servers = dns.servers || [];
  const max = Math.max(1, ...servers.map((s) => s.median_ms || 0));
  const rows = servers.map((server) => {
    const label = el('div', { class: 'bar-label' }, server.name, server.is_current ? el('span', { class: 'tag', text: 'current' }) : null);
    const blocked = !server.reachable;
    const track = el('div', { class: 'bar-track bar-row-hover', title: blocked ? `${server.name} (${server.ip}) did not answer` : `${server.name} (${server.ip}): median ${server.median_ms} ms, ${server.failures} of ${server.queries} lookups failed` },
      blocked ? null : el('i', { style: { width: `${(server.median_ms / max) * 100}%` } }));
    const value = el('div', { class: 'bar-value', text: blocked ? 'blocked' : `${num(server.median_ms)} ms` });
    return [label, track, value];
  });
  fill('dns-card',
    el('h2', { text: 'DNS lookup time' }),
    el('p', { class: 'muted small', text: 'How long each DNS server takes to answer from this network.' }),
    el('div', { class: 'bars' }, rows),
    el('p', { class: 'axis-note', text: dns.public_blocked ? 'This network blocks public DNS servers, so only its own DNS can be used.' : 'Median of 8 lookups per server. Shorter is better.' }));
}

function renderLatency() {
  const latency = state.results.latency;
  const row = (name, stats) => el('tr', {},
    el('td', { text: name }),
    el('td', { text: stats.method === 'tcp' ? 'TCP handshake' : 'Ping' }),
    el('td', { class: 'num', text: stats.received ? `${num(stats.avg_ms)} ms` : 'no reply' }),
    el('td', { class: 'num', text: stats.received ? `${num(stats.jitter_ms)} ms` : '—' }),
    el('td', { class: 'num', text: stats.received ? `${num(stats.loss_pct)}%` : '—' }));
  const rows = [];
  if (latency.gateway) rows.push(row(`Router (${latency.gateway.host})`, latency.gateway));
  for (const target of latency.targets || []) rows.push(row(`${target.name} (${target.host})`, target));
  fill('latency-card',
    el('h2', { text: 'Latency by hop' }),
    el('p', { class: 'muted small', text: 'A slow router row means the Wi-Fi link; slow rows below it mean the network beyond.' }),
    el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, el('th', { text: 'Target' }), el('th', { text: 'Method' }), el('th', { class: 'num', text: 'Average' }), el('th', { class: 'num', text: 'Jitter' }), el('th', { class: 'num', text: 'Loss' }))),
      el('tbody', {}, rows))),
    latency.icmp_blocked ? el('p', { class: 'axis-note', text: 'This network blocks pings to the internet, so website connection times are used instead.' }) : null);
}

function signalCell(percent) {
  return el('td', {}, el('span', { class: 'signal' }, el('i', { style: { width: `${percent || 0}%` } })), percent != null ? `${percent}%` : '—');
}

function renderNearby() {
  const nearby = state.results.nearby;
  if (nearby.skipped) { $('nearby-card').hidden = true; return; }
  const head = [el('h2', { text: 'Wi-Fi networks in range' })];
  if (nearby.error) {
    fill('nearby-card', head, el('div', { class: 'hint', text: nearby.message }));
    return;
  }
  const mine = state.conn && state.conn.wifi ? state.conn.wifi.bssid : null;
  const networks = [...nearby.networks].sort((a, b) => (b.signal || 0) - (a.signal || 0)).slice(0, 30);
  const rows = networks.map((net) => el('tr', { class: net.bssid === mine ? 'mine' : null },
    el('td', {}, net.ssid || '(hidden network)', net.bssid === mine ? el('span', { class: 'badge', style: { marginLeft: '8px' }, text: 'connected' }) : null),
    el('td', { text: net.band ? `${net.band} GHz` : '—' }),
    el('td', { class: 'num', text: net.channel ?? '—' }),
    signalCell(net.signal),
    el('td', { text: net.radio || '—' }),
    el('td', { class: 'num', text: net.utilization != null ? `${net.utilization}%` : '—' })));
  const summary = nearby.summary || {};
  const note = summary.same_channel != null ? `${summary.same_channel} other access points overlap your channel.` : null;
  fill('nearby-card', head,
    el('p', { class: 'muted small', text: `${nearby.networks.length} access points seen, strongest first. ${note || ''}` }),
    el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, el('th', { text: 'Network' }), el('th', { text: 'Band' }), el('th', { class: 'num', text: 'Channel' }), el('th', { text: 'Signal' }), el('th', { text: 'Standard' }), el('th', { class: 'num', text: 'Airtime in use' }))),
      el('tbody', {}, rows))));
}

function renderBackground() {
  const bg = state.results.background;
  if (bg.error) {
    fill('background-card', el('h2', { text: 'Background usage' }), el('div', { class: 'hint', text: bg.error }));
    return;
  }
  const rows = (bg.apps || []).map((app) => el('tr', {},
    el('td', { text: app.name }),
    el('td', { class: 'num', text: app.connections }),
    el('td', { text: app.note || '—' })));
  fill('background-card',
    el('h2', { text: 'Background usage' }),
    el('p', { class: 'muted small', text: `Traffic while no test was running: ${mbps(bg.down_bps)} Mbps down, ${mbps(bg.up_bps)} Mbps up. Apps holding internet connections:` }),
    rows.length ? el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, el('th', { text: 'App' }), el('th', { class: 'num', text: 'Connections' }), el('th', { text: 'Known for' }))),
      el('tbody', {}, rows))) : el('p', { class: 'muted small', text: 'No apps with open internet connections.' }));
}

// ---------------------------------------------------------------- fixes

const STATE_PILL = {
  on: ['good', 'Optimized'],
  off: ['off', 'Not applied'],
  unsupported: ['info', 'Not available here'],
  error: ['bad', 'Could not check'],
};

async function loadFixes(quiet) {
  if (!quiet) fill('fixes-list', el('p', { class: 'muted', text: 'Checking current settings…' }));
  try {
    const data = await api('GET', '/api/fixes');
    state.fixes = data.fixes;
    state.actions = data.actions;
    state.admin = data.admin;
    renderFixes();
  } catch (err) {
    if (!quiet) fill('fixes-list', el('div', { class: 'hint', text: err.message }));
    else throw err;
  }
}

function renderFixes() {
  fill('fixes-list', state.fixes.map((fix) => {
    const status = fix.status;
    const [kind, label] = STATE_PILL[status.state] || STATE_PILL.error;
    const controls = [];
    const locked = fix.requires_admin && !state.admin;
    if (status.state === 'off') {
      let select = null;
      if (fix.id === 'dns') {
        select = el('select', { 'aria-label': 'DNS provider' }, el('option', { value: 'auto', text: 'Fastest (auto)' }),
          (status.options || []).map((o) => el('option', { value: o.id, text: o.name })));
        controls.push(select);
      }
      controls.push(el('button', {
        class: 'btn primary',
        onclick: async (event) => {
          event.target.disabled = true;
          await changeFix(fix.id, 'apply', select ? { provider: select.value } : {});
          event.target.disabled = false;
        },
      }, locked ? 'Apply (needs admin)' : 'Apply'));
    } else if (status.state === 'on' && status.managed) {
      controls.push(el('button', {
        class: 'btn',
        onclick: async (event) => {
          event.target.disabled = true;
          await changeFix(fix.id, 'revert');
          event.target.disabled = false;
        },
      }, locked ? 'Revert (needs admin)' : 'Revert'));
    }
    const badges = [];
    if (fix.requires_admin) badges.push(el('span', { class: 'badge', text: 'Needs administrator' }));
    if (fix.drops_connection) badges.push(el('span', { class: 'badge', text: 'Wi-Fi reconnects briefly' }));
    if (status.state === 'on' && !status.managed) badges.push(el('span', { class: 'badge', text: 'Already set, not by NetTune' }));
    return el('div', { class: 'card fix' },
      el('div', {},
        el('div', { class: 'fix-title' }, fix.title, pill(kind, label)),
        el('div', { class: 'fix-summary', text: fix.summary }),
        el('div', { class: 'fix-state', text: status.detail }),
        badges.length ? el('div', { class: 'badges' }, badges) : null),
      el('div', { class: 'fix-controls' }, controls),
      el('details', {}, el('summary', { text: 'Why it helps and what it costs' }), el('p', { text: fix.details })));
  }));
  fill('actions-list', state.actions.map((action) => el('div', { class: 'card fix' },
    el('div', {},
      el('div', { class: 'fix-title', text: action.title }),
      el('div', { class: 'fix-summary', text: action.summary })),
    el('div', { class: 'fix-controls' }, el('button', { class: 'btn', onclick: () => runAction(action.id) }, 'Run')))));
}

async function changeFix(id, verb, params) {
  const meta = state.fixes.find((f) => f.id === id);
  if (meta && meta.requires_admin && !state.admin) { elevate(); return false; }
  if (meta && meta.drops_connection && !confirm(`${meta.title}\n\nThe Wi-Fi connection drops for a few seconds while the driver restarts. Continue?`)) return false;
  try {
    const result = await api('POST', `/api/fixes/${id}/${verb}`, { params: params || {} });
    toast(result.message, 'good');
    await loadFixes(true).catch(() => {});
    return true;
  } catch (err) {
    if (err.code === 'needs_admin') elevate();
    else toast(err.message, 'bad');
    return false;
  }
}

async function runAction(id) {
  const meta = state.actions.find((a) => a.id === id);
  if (meta && meta.drops_connection && !confirm(`${meta.title}\n\nThe Wi-Fi connection drops for a few seconds. Continue?`)) return;
  try {
    const result = await api('POST', `/api/actions/${id}`);
    toast(result.message, 'good');
    setTimeout(loadStatus, 4000);
  } catch (err) {
    toast(err.message, 'bad');
  }
}

// ---------------------------------------------------------------- combine

const combine = { timer: null, last: null, lastAt: 0, proxy: true };

async function loadCombine(quiet) {
  clearInterval(combine.timer);
  if (!quiet) fill('combine-card', el('p', { class: 'muted', text: 'Looking for connections…' }));
  let status;
  try {
    status = await api('GET', '/api/combine');
  } catch (err) {
    fill('combine-card', el('div', { class: 'hint', text: err.message }));
    return;
  }
  renderCombine(status);
  if (status.running) combine.timer = setInterval(() => { if (!$('tab-combine').hidden) loadCombine(true); }, 2000);
}

function renderCombine(status) {
  const now = performance.now();
  const seconds = combine.last ? (now - combine.lastAt) / 1000 : 0;
  const rate = (uplink, key) => {
    const before = combine.last && combine.last.uplinks.find((u) => u.if_index === uplink.if_index);
    if (!status.running || !before || seconds <= 0) return null;
    return Math.max(0, (uplink[key] - before[key]) * 8 / seconds / 1e6);
  };
  let totalDown = 0;
  const rows = status.uplinks.map((uplink) => {
    const down = rate(uplink, 'bytes_down');
    const up = rate(uplink, 'bytes_up');
    totalDown += down || 0;
    const state = uplink.down ? pill('bad', 'Failing') : uplink.online ? pill('good', 'Online') : pill('bad', 'No internet');
    return el('tr', {},
      el('td', {}, el('div', { style: { fontWeight: '600' }, text: uplink.name }), el('div', { class: 'muted small', text: uplink.description })),
      el('td', {}, state),
      el('td', { class: 'num', text: uplink.latency_ms != null ? `${num(uplink.latency_ms)} ms` : '—' }),
      el('td', { class: 'num', text: status.running ? uplink.active : '—' }),
      el('td', { class: 'num', text: down != null ? `${num(down, 2)} Mbps` : '—' }),
      el('td', { class: 'num', text: up != null ? `${num(up, 2)} Mbps` : '—' }),
      el('td', { class: 'num', text: status.running ? `${num((uplink.bytes_down + uplink.bytes_up) / 1e6, 1)} MB` : '—' }));
  });
  combine.last = status;
  combine.lastAt = now;

  const proxyBox = el('input', { type: 'checkbox', checked: combine.proxy, onchange: (event) => { combine.proxy = event.target.checked; } });
  const controls = status.running
    ? [el('button', { class: 'btn', onclick: stopCombine }, 'Stop combining')]
    : [el('label', { class: 'check', title: 'Sets the Windows proxy to NetTune so browsers and most apps use both connections. Restored when you stop.' }, proxyBox, 'Send Windows apps through it'),
       el('button', { class: 'btn primary', disabled: !status.ready, onclick: startCombine }, 'Start combining')];
  const online = status.uplinks.filter((u) => u.online).length;
  const headline = status.running
    ? [dot('good'), `Combining ${status.uplinks.length} connections`]
    : status.ready ? [dot('info'), `${online} connections ready to combine`]
      : [dot('off'), `${online} connection found — a second one is needed`];
  const detail = status.running
    ? `Proxy on 127.0.0.1:${status.port} (HTTP and SOCKS5). ${status.system_proxy ? 'Windows apps are using it.' : 'Windows proxy not changed: point apps at this address yourself.'} Now: ${num(totalDown, 2)} Mbps down in total.`
    : null;
  fill('combine-card',
    el('div', { class: 'combine-bar' }, el('div', { class: 'combine-state' }, headline), el('div', { class: 'combine-controls' }, controls)),
    detail ? el('div', { class: 'combine-total', text: detail }) : null,
    status.uplinks.length ? el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, el('th', { text: 'Connection' }), el('th', { text: 'State' }), el('th', { class: 'num', text: 'Latency' }), el('th', { class: 'num', text: 'Open now' }), el('th', { class: 'num', text: 'Down' }), el('th', { class: 'num', text: 'Up' }), el('th', { class: 'num', text: 'Data used' }))),
      el('tbody', {}, rows))) : el('div', { class: 'hint', text: 'No internet connection found.' }));
}

async function startCombine(event) {
  const message = combine.proxy
    ? 'Start combining?\n\nNetTune will set the Windows proxy to itself so apps use both connections, and put your previous proxy setting back when you stop. Your mobile data will be used.'
    : 'Start combining?\n\nYour mobile data will be used.';
  if (!confirm(message)) return;
  event.target.disabled = true;
  try {
    combine.last = null;
    renderCombine(await api('POST', '/api/combine/start', { system_proxy: combine.proxy }));
    toast('Combining started.', 'good');
    loadCombine(true);
  } catch (err) {
    toast(err.message, 'bad');
    event.target.disabled = false;
  }
}

async function stopCombine(event) {
  event.target.disabled = true;
  try {
    await api('POST', '/api/combine/stop');
    toast('Combining stopped.', 'good');
  } catch (err) {
    toast(err.message, 'bad');
  }
  combine.last = null;
  loadCombine();
}

// ---------------------------------------------------------------- profiles

async function loadProfiles(outcomes) {
  let data;
  try {
    data = await api('GET', '/api/profiles');
    if (!state.fixes.length) await loadFixes(true);
  } catch (err) {
    fill('binding-card', el('div', { class: 'hint', text: err.message }));
    return;
  }
  const bound = data.ssid ? data.bindings[data.ssid] || '' : '';
  const select = el('select', { 'aria-label': 'Profile for this network', onchange: async (event) => {
    try { await api('POST', '/api/profiles/bind', { ssid: data.ssid, profile: event.target.value || null }); toast('Saved.', 'good'); loadProfiles(); }
    catch (err) { toast(err.message, 'bad'); }
  } }, el('option', { value: '', text: 'No profile' }), data.profiles.map((p) => el('option', { value: p.id, text: p.name, selected: p.id === bound })));
  const auto = el('input', { type: 'checkbox', checked: data.auto, onchange: async (event) => {
    try { await api('POST', '/api/profiles/auto', { enabled: event.target.checked }); toast(event.target.checked ? 'Profiles now apply automatically while NetTune is running.' : 'Automatic profiles turned off.', 'good'); }
    catch (err) { toast(err.message, 'bad'); }
  } });
  const others = Object.entries(data.bindings).filter(([ssid]) => ssid !== data.ssid);
  fill('binding-card',
    el('h2', { text: 'Automatic profiles' }),
    data.ssid
      ? el('div', { class: 'bind-row' }, el('span', {}, 'When connected to ', el('b', { text: data.ssid }), ' use'), select)
      : el('div', { class: 'hint', text: 'Windows is hiding the Wi-Fi network name, so a profile cannot be tied to this network yet. Turn on Location services (Settings > Privacy & security > Location) and reload.' }),
    el('div', { class: 'bind-row' }, el('label', { class: 'check' }, auto, 'Apply the matching profile automatically when I join a network')),
    others.length ? el('p', { class: 'muted small', style: { marginTop: '10px' }, text: 'Other networks: ' + others.map(([ssid, pid]) => `${ssid} → ${(data.profiles.find((p) => p.id === pid) || {}).name || pid}`).join(', ') }) : null);

  fill('profiles-list', data.profiles.map((profile) => {
    const boxes = state.fixes.map((fix) => el('label', { class: 'check' },
      el('input', { type: 'checkbox', checked: profile.fixes.includes(fix.id), 'data-fix': fix.id, onchange: async () => {
        const chosen = [...card.querySelectorAll('input[data-fix]')].filter((box) => box.checked).map((box) => box.dataset.fix);
        try { await api('POST', `/api/profiles/${profile.id}`, { fixes: chosen }); } catch (err) { toast(err.message, 'bad'); }
      } }), fix.title));
    const result = outcomes && outcomes.id === profile.id ? el('div', { class: 'outcomes' }, outcomes.items.length
      ? outcomes.items.map((o) => el('div', { class: 'outcome' }, dot({ applied: 'good', already: 'good', reverted: 'info', skipped: 'off', failed: 'bad' }[o.outcome] || 'off'), el('span', {}, el('b', { text: `${o.title}: ` }), `${o.outcome}. ${o.message || ''}`)))
      : el('span', { class: 'muted', text: 'Nothing needed changing.' })) : null;
    const card = el('div', { class: 'card profile' },
      el('h2', { text: profile.name }),
      el('p', { class: 'muted small', text: profile.description }),
      el('div', { class: 'profile-fixes' }, boxes),
      el('button', { class: 'btn primary', onclick: async (event) => {
        if (!confirm(`Apply the "${profile.name}" profile?\n\nFixes that restart the Wi-Fi driver will drop the connection for a few seconds.`)) return;
        event.target.disabled = true;
        try {
          const response = await api('POST', `/api/profiles/${profile.id}/apply`);
          await loadFixes(true).catch(() => {});
          await loadProfiles({ id: profile.id, items: response.outcomes });
        } catch (err) { toast(err.message, 'bad'); event.target.disabled = false; }
      } }, 'Apply profile'),
      !state.admin ? el('p', { class: 'muted small', style: { marginTop: '8px' }, text: 'Without administrator rights, fixes that need them are skipped.' }) : null,
      result);
    return card;
  }));
}

// ---------------------------------------------------------------- history

const METRICS = [
  ['download_mbps', 'Download', 'Mbps', true],
  ['upload_mbps', 'Upload', 'Mbps', true],
  ['internet_ms', 'Ping', 'ms', false],
  ['jitter_ms', 'Jitter', 'ms', false],
  ['loss_pct', 'Loss', '%', false],
  ['dns_ms', 'DNS', 'ms', false],
  ['gateway_ms', 'Router', 'ms', false],
];

function delta(current, previous, higherIsBetter) {
  if (current == null || previous == null) return null;
  const diff = current - previous;
  if (Math.abs(diff) < 0.05) return null;
  const better = higherIsBetter ? diff > 0 : diff < 0;
  return el('span', { class: `delta ${better ? 'better' : 'worse'}`, title: `${better ? 'Better' : 'Worse'} than the previous run on this network`, text: `${diff > 0 ? '▲' : '▼'} ${num(Math.abs(diff))} ${better ? 'better' : 'worse'}` });
}

async function loadHistory() {
  let entries;
  try {
    entries = (await api('GET', '/api/history')).entries;
  } catch (err) {
    fill('history-card', el('div', { class: 'hint', text: err.message }));
    return;
  }
  if (!entries.length) {
    fill('history-card', el('p', { class: 'muted', text: 'No runs yet. Run a diagnosis, apply a fix, then run it again to see the difference here.' }));
    return;
  }
  const when = (seconds) => new Date(seconds * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const rows = entries.map((entry, index) => {
    if (entry.type === 'event') {
      return el('tr', { class: 'event-row' }, el('td', { text: when(entry.time) }), el('td', { text: entry.ssid || '—' }), el('td', { colspan: String(METRICS.length), text: entry.text }));
    }
    const previous = entries.slice(index + 1).find((e) => e.type === 'run' && e.ssid === entry.ssid);
    return el('tr', {}, el('td', { text: when(entry.time) }), el('td', { text: entry.ssid || '—' }),
      METRICS.map(([key, , unit, higher]) => el('td', { class: 'num' }, entry[key] != null ? `${num(entry[key])} ${unit}` : '—', previous ? delta(entry[key], previous[key], higher) : null)));
  });
  fill('history-card', el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {}, el('th', { text: 'When' }), el('th', { text: 'Network' }), METRICS.map(([, label]) => el('th', { class: 'num', text: label })))),
    el('tbody', {}, rows))));
}

// ---------------------------------------------------------------- wiring

for (const button of document.querySelectorAll('.tabs button')) button.addEventListener('click', () => showTab(button.dataset.tab));
$('run').addEventListener('click', runDiagnosis);
$('fixes-refresh').addEventListener('click', () => loadFixes());
$('combine-refresh').addEventListener('click', () => loadCombine());
$('history-clear').addEventListener('click', async () => {
  if (!confirm('Delete all saved runs? This cannot be undone.')) return;
  try { await api('DELETE', '/api/history'); loadHistory(); } catch (err) { toast(err.message, 'bad'); }
});
$('quit').addEventListener('click', async () => {
  if (!confirm('Stop the NetTune engine? Applied fixes stay in place.')) return;
  try { await api('POST', '/api/shutdown'); } catch (err) { /* already gone */ }
  overlay('NetTune has stopped. You can close this tab.');
  document.querySelector('.overlay-box .spinner').hidden = true;
});
$('opt-speed').addEventListener('change', (event) => { $('opt-mode').disabled = !event.target.checked; });

loadStatus().then(() => loadFixes(true).catch(() => {}));
