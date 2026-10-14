import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import './styles.css';

const $ = id => document.getElementById(id);
const terminal = new Terminal({ fontSize: 13, fontFamily: 'Menlo, monospace', scrollback: 10000,
  theme: { background: '#0b1119', foreground: '#dbe6ef', cursor: '#72d8bc' }, cursorBlink: true });
const fit = new FitAddon();
terminal.loadAddon(fit);
terminal.open($('terminal'));
let serial = 0, rows = [], selected = null, selectionEpoch = 0, defaults, currentScene;
let graphReady = false, graphVisible = false, jobPending = false, live = false;
const pending = new Map();
function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }
function rpc(method, args = {}) {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    invoke('desktop_send', { message: { id, method, args } }).catch(error => {
      pending.delete(id); reject(new Error(String(error)));
    });
  });
}
function safely(action) { return async () => { try { await action(); } catch (error) { status(error.message || String(error), true); } }; }
function decode(data) { return Uint8Array.from(atob(data), c => c.charCodeAt(0)); }
function encode(data) { return btoa(Array.from(new TextEncoder().encode(data), c => String.fromCharCode(c)).join('')); }
terminal.onData(data => rpc('input', { data: encode(data) }).catch(error => status(String(error), true)));
terminal.onBinary(data => rpc('input', { data: btoa(data) }).catch(error => status(String(error), true)));
function resize() {
  fit.fit();
  rpc('resize', { rows: terminal.rows, columns: terminal.cols }).catch(() => {});
}
new ResizeObserver(resize).observe($('terminal'));
function showGraph(show) {
  graphVisible = show;
  $('source').hidden = show; $('graph').hidden = !show;
  $('source-tab').classList.toggle('selected', !show);
  $('graph-tab').classList.toggle('selected', show);
}
function sendScene() {
  if (graphReady && currentScene) $('graph').contentWindow.postMessage({ type: 'labradour-scene', scene: currentScene }, window.location.origin);
}
window.addEventListener('message', event => {
  if (event.source !== $('graph').contentWindow || event.data?.type !== 'gitdiffviz-ready') return;
  graphReady = true; sendScene();
});
function matches(row) {
  const text = ['actor','turn','tool','state','path','target'].map(key => row[key] || '').join(' ').toLowerCase();
  return $('filter').value.toLowerCase().split(/\s+/).filter(Boolean).every(term => {
    const colon = term.indexOf(':');
    const key = term.slice(0, colon);
    return colon > 0 && ['actor','turn','tool','state','path'].includes(key)
      ? String(row[key] || '').toLowerCase().includes(term.slice(colon + 1)) : text.includes(term);
  });
}
function allRows() { return rows.flatMap(row => [row, ...row.children]); }
function renderRows() {
  const pane = $('activity'), position = pane.scrollTop;
  const fragment = document.createDocumentFragment();
  for (const row of allRows().filter(matches)) {
    const button = document.createElement('button');
    button.className = 'row ' + row.kind + (selected?.id === row.id ? ' active' : '');
    const label = document.createElement('div'); label.className = 'label';
    const path = document.createElement('span'); path.className = 'path'; path.textContent = row.path || row.target || row.tool;
    const tool = document.createElement('span'); tool.textContent = row.tool;
    label.append(path, tool);
    const meta = document.createElement('div'); meta.className = 'meta'; meta.textContent = `${row.sequence} · ${row.state} · ${row.actor}`;
    button.append(label, meta);
    button.onclick = safely(async () => { $('follow').checked = false; await selectRow(row); });
    fragment.append(button);
  }
  pane.replaceChildren(fragment); pane.scrollTop = position;
  $('row-count').textContent = `· ${allRows().length}`;
}
async function runJob(operation) {
  if (!selected && operation !== 'session-export') throw new Error('Select a recorded row first');
  if (jobPending) throw new Error('A view/export is pending; cancel it or wait');
  jobPending = true;
  const epoch = selectionEpoch, row = selected;
  status(operation === 'gitdiffviz' ? 'Preparing captured GitDiffViz scene…' : 'Preparing captured evidence…');
  try {
    const result = await rpc('job', { operation, row: row?.id });
    if (operation === 'view' && epoch === selectionEpoch) {
      const heading = document.createElement('h2'); heading.textContent = result.title;
      const pre = document.createElement('pre'); pre.textContent = result.lines.join('\n');
      $('source').replaceChildren(heading, pre);
    } else if (operation === 'gitdiffviz' && epoch === selectionEpoch) {
      currentScene = result.scene; sendScene(); showGraph(true);
      $('selection-info').textContent = `${row.path} · captured interval ${result.provenance.before_commit?.slice(0, 8)} → ${result.provenance.checkpoint?.slice(0, 8)} · structural scene · external/unknown attribution`;
    }
    status(result.path ? `Ready · ${result.cached ? 'cached · ' : ''}${result.path}` : 'Captured evidence ready');
    return result;
  } finally { jobPending = false; }
}
async function selectRow(row) {
  selected = row; selectionEpoch++;
  const epoch = selectionEpoch;
  renderRows();
  $('selection-info').textContent = `${row.path || row.tool} · ${row.state} · ${row.actor}`;
  currentScene = null;
  $('source').textContent = 'Preparing captured evidence…';
  // Hide the previous scene immediately; a failed or stale job must not display it as this row.
  showGraph(false);
  if (jobPending) { await rpc('cancel'); status('Previous view cancelled; select again when it finishes'); return; }
  await runJob('view');
  if (epoch === selectionEpoch && row.kind === 'effect' && $('graph-tab').dataset.auto === 'true') await runJob('gitdiffviz');
}
await listen('desktop-message', ({ payload: message }) => {
  if (message.id !== undefined) {
    const request = pending.get(message.id);
    if (request) { pending.delete(message.id); message.error ? request.reject(new Error(message.error)) : request.resolve(message.result); }
    return;
  }
  const data = message.payload;
  if (message.event === 'terminal') terminal.write(decode(data.data));
  else if (message.event === 'activity') {
    rows = data.rows;
    $('recording-path').textContent = data.recording;
    live = data.status === 'live'; $('stop').disabled = !['live','exited'].includes(data.status);
    for (const id of ['demo', 'sample', 'launch', 'open-recording']) $(id).disabled = !$('stop').disabled;
    $('agent-state').textContent = data.status === 'saved' ? 'Saved review · no agent launched' : data.status;
    $('session-export').disabled = live;
    const choice = $('session').value;
    $('session').replaceChildren(new Option('Latest session', ''), ...data.sessions.map(s => new Option(`${s.id.slice(0, 12)} · ${s.status}`, s.id)));
    $('session').value = choice;
    renderRows();
    if ($('follow').checked && !jobPending) {
      const latest = allRows().filter(row => row.kind === 'effect').at(-1) || allRows().at(-1);
      if (latest && latest.id !== selected?.id) selectRow(latest).catch(error => status(error.message, true));
      $('activity').scrollTop = $('activity').scrollHeight;
    }
    status(data.health);
  } else if (message.event === 'exit') status(`Agent exited ${data.code} · recorded review remains available`);
  else if (message.event === 'error') status(data.message, true);
  else if (message.event === 'backend-exit') {
    for (const request of pending.values()) request.reject(new Error('Desktop backend exited'));
    pending.clear(); status('Desktop backend exited. Reopen Labradour.', true);
  }
});
defaults = await invoke('desktop_defaults');
$('workspace').value = defaults.workspace; $('config').value = defaults.config;
const baseArgs = () => ({ config: $('config').value, rows: terminal.rows, columns: terminal.cols });
function resetSelection() { selected = null; selectionEpoch++; currentScene = null; showGraph(false); $('source').textContent = 'Select captured evidence to inspect it.'; }
$('demo').onclick = safely(async () => {
  resetSelection(); terminal.reset(); $('follow').checked = true;
  const result = await rpc('launch', { ...baseArgs(), demo: true });
  $('recording').value = result.recording; terminal.focus(); resize();
});
$('sample').onclick = safely(async () => {
  resetSelection(); $('follow').checked = false;
  await rpc('review', { ...baseArgs(), recording: defaults.recording, cache: defaults.cache });
  $('recording').value = defaults.recording;
  const edit = allRows().find(row => row.kind === 'effect' && row.tool === 'file.modify');
  if (edit) { await selectRow(edit); await runJob('gitdiffviz'); }
});
$('settings').onclick = () => $('launch-dialog').showModal();
for (const name of ['workspace','recording']) $('browse-' + name).onclick = safely(async () => {
  const path = await invoke('choose_directory'); if (path) $(name).value = path;
});
$('launch').onclick = safely(async () => {
  const provider = $('provider').value;
  resetSelection(); terminal.reset(); $('follow').checked = true;
  const result = await rpc('launch', { ...baseArgs(), workspace: $('workspace').value,
    recording: $('recording').value, provider, model: $('model').value.trim(),
    hooks: provider !== 'shell' && $('hooks').checked ? provider : null });
  $('recording').value = result.recording; $('launch-dialog').close(); terminal.focus(); resize();
});
$('open-recording').onclick = safely(async () => {
  resetSelection(); $('follow').checked = false;
  await rpc('review', { ...baseArgs(), recording: $('recording').value, session: $('session').value });
  $('launch-dialog').close();
});
$('provider').onchange = () => { $('model').disabled = $('provider').value === 'shell'; $('hooks').disabled = $('provider').value === 'shell'; };
$('stop').onclick = safely(async () => { await rpc('stop'); status('Agent stopped · final capture saved'); });
$('refresh').onclick = safely(async () => {
  if (live) return;
  await rpc('review', { ...baseArgs(), recording: $('recording').value, session: $('session').value });
});
$('source-tab').onclick = () => showGraph(false);
$('graph-tab').onclick = safely(async () => { $('graph-tab').dataset.auto = 'true'; await runJob('gitdiffviz'); });
$('export').onclick = safely(() => runJob('export'));
$('session-export').onclick = safely(() => runJob('session-export'));
$('cancel').onclick = safely(() => rpc('cancel'));
$('filter').oninput = renderRows;
function divider(id, horizontal) {
  $(id).onpointerdown = event => {
    $(id).setPointerCapture(event.pointerId);
    const move = e => {
      const bounds = (horizontal ? $('review-panel') : document.querySelector('main')).getBoundingClientRect();
      const percent = horizontal ? (e.clientY - bounds.top) / bounds.height * 100 : (e.clientX - bounds.left) / bounds.width * 100;
      document.documentElement.style.setProperty(horizontal ? '--activity-height' : '--agent-width', `${Math.max(20, Math.min(75, percent))}%`);
    };
    $(id).onpointermove = move;
    $(id).onpointerup = () => { $(id).onpointermove = null; resize(); };
  };
}
divider('vertical-divider', false); divider('horizontal-divider', true);
fit.fit();
