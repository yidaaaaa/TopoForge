let english = navigator.language.toLowerCase().startsWith('en');
try {
  const saved = localStorage.getItem('topoforge-language');
  if (saved === 'en' || saved === 'zh-CN') english = saved === 'en';
} catch { /* The viewer also works without browser storage. */ }
const message = (zh, en) => english ? en : zh;
if (english) {
  document.documentElement.lang = 'en';
  document.title = 'TopoForge · Original reference map';
  const labels = {
    heading: 'Original reference map', subtitle: 'Local map reference',
    fit: 'Fit to window', actual: 'Actual size', download: 'Save original',
    status: 'Reading the local original…',
    gestures: 'Scroll to zoom · Drag to pan · Double-click to fit · Arrow keys to pan',
    usage: 'Reference image; select the print area on the terrain map.', source: 'Map source',
  };
  for (const [id, text] of Object.entries(labels)) document.getElementById(id).textContent = text;
  document.getElementById('plus').setAttribute('aria-label', 'Zoom in');
  document.getElementById('minus').setAttribute('aria-label', 'Zoom out');
  document.getElementById('viewport').setAttribute('aria-label', 'Zoomable original reference map');
  document.querySelector('nav').setAttribute('aria-label', 'Map viewing tools');
}
const pane = document.getElementById('viewport');
const map = document.getElementById('map');
const label = document.getElementById('scale');
const status = document.getElementById('status');
const tiles = new Map();
let metadata = null;
let scale = 1, x = 0, y = 0, drag = null, fitMode = true;

function showStatus(text) {
  status.textContent = text;
  status.style.display = text ? 'grid' : 'none';
}
function fitScale() {
  return Math.min((pane.clientWidth - 28) / metadata.width_px, (pane.clientHeight - 28) / metadata.height_px);
}
function checkTiles() {
  if ([...tiles.values()].some(tile => tile.dataset.failed)) {
    pane.dataset.ready = 'error';
    showStatus(message('原图图块未能加载，请检查本地文件后刷新。', 'Map tiles could not be loaded. Check the local files and refresh.'));
  } else if ([...tiles.values()].every(tile => tile.complete && tile.naturalWidth > 0)) {
    pane.dataset.ready = 'true';
    showStatus('');
  }
}
function render() {
  if (!metadata) return;
  label.value = `${Math.round(scale * 100)}%`;
  pane.dataset.scale = String(scale);
  const level = Math.max(0, Math.min(metadata.max_level,
    metadata.max_level + Math.ceil(Math.log2(scale * devicePixelRatio))));
  const divisor = 2 ** (metadata.max_level - level);
  const width = Math.ceil(metadata.width_px / divisor);
  const height = Math.ceil(metadata.height_px / divisor);
  const size = metadata.tile_size_px;
  const ratioX = metadata.width_px / width;
  const ratioY = metadata.height_px / height;
  const startX = Math.max(0, Math.floor(-x / (scale * ratioX * size)));
  const startY = Math.max(0, Math.floor(-y / (scale * ratioY * size)));
  const endX = Math.min(Math.ceil(width / size) - 1, Math.floor((pane.clientWidth - x) / (scale * ratioX * size)));
  const endY = Math.min(Math.ceil(height / size) - 1, Math.floor((pane.clientHeight - y) / (scale * ratioY * size)));
  const visible = new Set();
  for (let column = startX; column <= endX; column++) {
    for (let row = startY; row <= endY; row++) {
      const key = `${level}/${column}/${row}`;
      visible.add(key);
      let tile = tiles.get(key);
      if (!tile) {
        tile = document.createElement('img');
        tile.alt = '';
        tile.draggable = false;
        tile.decoding = 'async';
        tile.dataset.key = key;
        tile.onload = checkTiles;
        tile.onerror = () => { tile.dataset.failed = 'true'; checkTiles(); };
        tile.src = metadata.tile_url_template.replace('{level}', level).replace('{x}', column).replace('{y}', row);
        tiles.set(key, tile);
        map.append(tile);
      }
      tile.style.left = `${x + column * size * ratioX * scale}px`;
      tile.style.top = `${y + row * size * ratioY * scale}px`;
      tile.style.width = `${Math.min(size, width - column * size) * ratioX * scale}px`;
      tile.style.height = `${Math.min(size, height - row * size) * ratioY * scale}px`;
    }
  }
  for (const [key, tile] of tiles) {
    if (!visible.has(key)) { tile.remove(); tiles.delete(key); }
  }
  pane.dataset.level = String(level);
  pane.dataset.ready = 'loading';
  checkTiles();
}
function fit() {
  if (!metadata) return;
  scale = fitScale();
  x = (pane.clientWidth - metadata.width_px * scale) / 2;
  y = (pane.clientHeight - metadata.height_px * scale) / 2;
  fitMode = true;
  render();
}
function zoom(next, px = pane.clientWidth / 2, py = pane.clientHeight / 2) {
  if (!metadata) return;
  next = Math.max(Math.min(fitScale() / 2, 0.02), Math.min(4, next));
  const ratio = next / scale;
  x = px - (px - x) * ratio;
  y = py - (py - y) * ratio;
  scale = next;
  fitMode = false;
  render();
}
document.getElementById('fit').onclick = fit;
document.getElementById('actual').onclick = () => zoom(1);
document.getElementById('plus').onclick = () => zoom(scale * 1.4);
document.getElementById('minus').onclick = () => zoom(scale / 1.4);
pane.addEventListener('wheel', event => {
  event.preventDefault();
  const rect = pane.getBoundingClientRect();
  zoom(scale * Math.exp(-event.deltaY * .0015), event.clientX - rect.left, event.clientY - rect.top);
}, { passive: false });
pane.addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  drag = { id: event.pointerId, x: event.clientX, y: event.clientY };
  pane.setPointerCapture(event.pointerId);
  pane.classList.add('dragging');
});
pane.addEventListener('pointermove', event => {
  if (!drag || drag.id !== event.pointerId) return;
  x += event.clientX - drag.x;
  y += event.clientY - drag.y;
  drag = { id: event.pointerId, x: event.clientX, y: event.clientY };
  fitMode = false;
  render();
});
function release(event) {
  if (drag?.id !== event.pointerId) return;
  drag = null;
  pane.classList.remove('dragging');
}
pane.addEventListener('pointerup', release);
pane.addEventListener('pointercancel', release);
pane.addEventListener('dblclick', fit);
pane.addEventListener('keydown', event => {
  const deltas = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] };
  if (deltas[event.key]) {
    event.preventDefault();
    x += deltas[event.key][0]; y += deltas[event.key][1]; fitMode = false; render();
  } else if (event.key === '+' || event.key === '=') zoom(scale * 1.4);
  else if (event.key === '-') zoom(scale / 1.4);
  else if (event.key === '0') fit();
});
new ResizeObserver(() => { if (fitMode) fit(); else render(); }).observe(pane);

try {
  const response = await fetch('/api/v1/reference/standard-map', { cache: 'no-store' });
  if (!response.ok) throw new Error('metadata');
  metadata = await response.json();
  if (!metadata) throw new Error('missing');
  const imageUrl = new URL(metadata.image_url, location.origin);
  const tileUrl = new URL(metadata.tile_url_template, location.origin);
  if (imageUrl.origin !== location.origin || tileUrl.origin !== location.origin) throw new Error('origin');
  document.getElementById('subtitle').textContent = metadata.title;
  document.title = metadata.title + ' · TopoForge';
  map.setAttribute('aria-label', metadata.title);
  map.dataset.width = String(metadata.width_px);
  map.dataset.height = String(metadata.height_px);
  const download = document.getElementById('download');
  download.href = imageUrl.href; download.hidden = false;
  if (metadata.source_url) {
    const sourceUrl = new URL(metadata.source_url);
    if (!['http:', 'https:'].includes(sourceUrl.protocol)) throw new Error('source URL');
    const source = document.getElementById('source');
    source.href = sourceUrl.href; source.hidden = false;
  }
  fit();
} catch {
  metadata = null;
  pane.dataset.ready = 'error';
  showStatus(message('未能读取本地标准地图，请检查原图和来源信息后刷新。',
    'The local map could not be read. Check the image and source metadata, then refresh.'));
}
