'use strict';

/* MonoSpectro — interface logic.
   Browser-side state: current ROI, reference blank and half-marked calibration
   points. None of this survives a page reload except the ROI, which lives on
   the server. */

const FRAME_W = 1280, FRAME_H = 720;
const POLL_MS = 200;

let currentW = [], currentI = [], currentP = [];
let reference = null;              // { values, roiKey } from the last "Blank"
let roi = null;
let pendingRoi = null;             // sent once, not on every poll
let chart = null;
let isCalMode = false;
let manualPoints = [];
let sampleRows = 0;
let lastAxisKey = '';
let smoothWindow = 1;              // odd window size for the display-only moving average

const $ = (id) => document.getElementById(id);
const roiKey = (r) => r ? `${r.x1},${r.y1},${r.x2},${r.y2}` : '';

/* -------------------------------------------------------------- smoothing */
/* Centered moving average, display only. currentI (raw) is left untouched so
   the readout and the Save Abs export always reflect the real measurement. */
function smooth(values, window) {
  if (window <= 1 || values.length < 3) return values;
  const half = Math.floor(window / 2);
  const out = new Array(values.length);
  for (let i = 0; i < values.length; i++) {
    let sum = 0, n = 0;
    for (let k = -half; k <= half; k++) {
      const j = i + k;
      if (j >= 0 && j < values.length) { sum += values[j]; n++; }
    }
    out[i] = sum / n;
  }
  return out;
}

/* ------------------------------------------------------------------ chart */
function initChart() {
  chart = new Chart($('mainChart').getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        { label: 'Spectrum', data: [], borderColor: '#1baf7a', borderWidth: 2,
          pointRadius: 0, fill: false, order: 2 },
        { label: 'Marks', data: [], type: 'scatter', backgroundColor: '#e34948',
          pointRadius: 5, order: 1 }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#a1a1a8', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#6b6b73', maxTicksLimit: 14, autoSkip: true },
             grid: { color: '#2c2c31' } },
        y: { ticks: { color: '#6b6b73' }, grid: { color: '#2c2c31' },
             title: { display: true, text: 'Intensity (0–255)', color: '#6b6b73' } }
      },
      onHover: (e, els) => {
        if (!els.length) return;
        const i = els[0].index;
        if (currentW[i] === undefined) return;
        $('readout').textContent =
          `${currentW[i].toFixed(1)} nm   ·   ${currentI[i]}   ·   px ${currentP[i].toFixed(0)}`;
      },
      onClick: (e) => {
        if (!isCalMode) return;
        const els = chart.getElementsAtEventForMode(e, 'index', { intersect: false }, true);
        if (!els.length) return;
        const i = els[0].index;
        const answer = prompt(`Pixel ${currentP[i].toFixed(1)}\nWhat wavelength is this, in nm?`);
        if (answer === null) return;
        const nm = parseFloat(answer);
        if (!isFinite(nm) || nm <= 0) { alert('Enter a number in nanometers.'); return; }
        manualPoints.push({ pixel: currentP[i], wavelength: nm,
                            chartX: currentW[i].toFixed(1), chartY: currentI[i] });
        updateManualUI();
      }
    }
  });
}

/* -------------------------------------------------------------------- loop */
async function loop() {
  try {
    const body = pendingRoi ? { selection: pendingRoi } : {};
    const res = await fetch('/spectrum', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (pendingRoi) { alert('ROI rejected: ' + (err.message || res.status)); pendingRoi = null; }
    } else {
      const data = await res.json();
      pendingRoi = null;
      applyData(data);
    }
  } catch (e) { /* the Pi can be slow to respond; retried next cycle */ }
  setTimeout(loop, POLL_MS);
}

function applyData(data) {
  if (!roi || roiKey(data.roi) !== roiKey(roi)) {
    roi = data.roi;
    $('roi1').value = `${roi.x1};${roi.y1}`;
    $('roi2').value = `${roi.x2};${roi.y2}`;
  }

  currentW = data.wavelengths;
  currentI = data.intensities;
  currentP = data.pixels;

  // Rebuilding the labels is expensive with ~1000 points. Only redo it when
  // the axis actually changes, not five times a second.
  const axisKey = `${currentW.length}|${data.is_calibrated}|${(data.coeffs || []).join(',')}|${roiKey(roi)}`;
  if (axisKey !== lastAxisKey) {
    chart.data.labels = currentW.map((w) => w.toFixed(1));
    lastAxisKey = axisKey;
  }
  chart.data.datasets[0].data = smooth(currentI, smoothWindow);
  chart.data.datasets[1].data = manualPoints.map((p) => ({ x: p.chartX, y: p.chartY }));
  chart.update('none');

  const badge = $('calStatus');
  badge.textContent = data.is_calibrated ? 'CALIBRATED' : 'GENERIC';
  badge.className = 'badge ' + (data.is_calibrated ? 'badge-ok' : 'badge-none');
  $('calEquation').textContent = formatEquation(data.coeffs);

  const banner = $('cameraError');
  banner.hidden = !data.camera_error;
  if (data.camera_error) banner.textContent = 'Camera unavailable — ' + data.camera_error;

  drawROI();
}

function formatEquation(coeffs) {
  if (!coeffs || !coeffs.length) return 'not calibrated · assuming 350–750 nm axis';
  const deg = coeffs.length - 1;
  let out = '';
  coeffs.forEach((c, i) => {
    const power = deg - i;
    const mag = Math.abs(c);
    const num = mag < 1e-4 ? mag.toExponential(2) : mag.toFixed(power === 2 ? 6 : 4);
    const term = num + (power === 2 ? '·x²' : power === 1 ? '·x' : '');
    if (i === 0) out = (c < 0 ? '-' : '') + term;
    else out += (c < 0 ? ' - ' : ' + ') + term;
  });
  return 'λ = ' + out + ' nm';
}

/* ---------------------------------------------------------------------- ROI */
/* The image is drawn with object-fit: contain, so it only fills part of the
   element and is centered. Scaling by clientWidth/1280 alone — like the
   previous version did — puts the rectangle somewhere else: in the lab it
   looked like the measurement was reading the bottom of the frame when it
   was really reading the center. */
function videoGeometry(img) {
  const nw = img.naturalWidth || FRAME_W;
  const nh = img.naturalHeight || FRAME_H;
  const cw = img.clientWidth, ch = img.clientHeight;
  if (!cw || !ch) return null;
  const scale = Math.min(cw / nw, ch / nh);
  return { scale, offX: (cw - nw * scale) / 2, offY: (ch - nh * scale) / 2 };
}

function drawROI() {
  const img = $('videoFeed'), box = $('roiBox');
  if (!img || !roi) return;
  const g = videoGeometry(img);
  if (!g) return;
  box.style.left   = (g.offX + roi.x1 * g.scale) + 'px';
  box.style.top    = (g.offY + roi.y1 * g.scale) + 'px';
  box.style.width  = ((roi.x2 - roi.x1) * g.scale) + 'px';
  box.style.height = ((roi.y2 - roi.y1) * g.scale) + 'px';
}

function parseCorner(el) {
  const parts = el.value.split(';');
  if (parts.length !== 2) return null;
  const x = parseInt(parts[0], 10), y = parseInt(parts[1], 10);
  if (!isFinite(x) || !isFinite(y)) return null;
  return { x, y };
}

function updateROI() {
  const a = $('roi1'), b = $('roi2');
  const p1 = parseCorner(a), p2 = parseCorner(b);
  a.classList.toggle('invalid', !p1);
  b.classList.toggle('invalid', !p2);
  if (!p1 || !p2) { alert('Write each corner as  x;y  — for example 100;300'); return; }

  const next = {
    x1: Math.max(0, Math.min(p1.x, p2.x)), y1: Math.max(0, Math.min(p1.y, p2.y)),
    x2: Math.min(FRAME_W, Math.max(p1.x, p2.x)), y2: Math.min(FRAME_H, Math.max(p1.y, p2.y))
  };
  if (next.x2 - next.x1 < 2 || next.y2 - next.y1 < 2) {
    alert('The rectangle is too small.'); return;
  }

  // Changing the ROI changes how many points the spectrum has: the previous
  // blank is no longer comparable and has to be retaken.
  if (reference && reference.roiKey !== roiKey(next)) {
    reference = null;
    $('btnAbs').disabled = true;
    alert('You changed the ROI. Take a new blank before measuring.');
  }
  pendingRoi = next;
}

/* -------------------------------------------------------------- blank & data */
function takeRef() {
  if (!currentI.length) { alert('No spectrum from the camera yet.'); return; }
  reference = { values: currentI.slice(), roiKey: roiKey(roi) };
  $('btnAbs').disabled = false;
  $('readout').textContent = `Blank taken · ${reference.values.length} points`;
}

function saveAbs() {
  if (!reference) { alert('Take a blank first.'); return; }
  if (reference.values.length !== currentI.length) {
    alert('The blank has a different length than the current spectrum. Take it again.');
    return;
  }
  const rows = ['Pixel,Wavelength_nm,abs'];
  for (let i = 0; i < currentI.length; i++) {
    const a = Math.log10(Math.max(reference.values[i], 1) / Math.max(currentI[i], 1));
    rows.push(`${currentP[i].toFixed(2)},${currentW[i].toFixed(2)},${a.toFixed(4)}`);
  }
  const url = URL.createObjectURL(new Blob([rows.join('\n')], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `abs_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

/* --------------------------------------------------------------- calibration */
function toggleCalMode() {
  isCalMode = !isCalMode;
  $('chartContainer').classList.toggle('cal-active', isCalMode);
  $('calBtn').textContent = isCalMode ? 'Click on the peaks' : 'Calibrate on chart';
}

function updateManualUI() {
  const list = $('puntosLista');
  list.innerHTML = '';
  manualPoints.forEach((p, i) => {
    const li = document.createElement('li');
    const span = document.createElement('span');
    span.textContent = `px ${p.pixel.toFixed(1)} → ${p.wavelength} nm`;
    const del = document.createElement('button');
    del.textContent = '×';
    del.title = 'Remove this point';
    del.onclick = () => { manualPoints.splice(i, 1); updateManualUI(); };
    li.append(span, del);
    list.appendChild(li);
  });
  $('pointCount').textContent = manualPoints.length;
  $('pointsEmpty').hidden = manualPoints.length > 0;
  $('btnSubmitCal').disabled = manualPoints.length < 2;
}

function clearPoints() { manualPoints = []; updateManualUI(); }

async function submitManualCal() {
  const res = await fetch('/calibrate_manual', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ points: manualPoints })
  });
  const data = await res.json().catch(() => ({}));
  if (data.status === 'success') location.reload();
  else alert('Could not calibrate: ' + (data.message || res.status));
}

function resetCalibration() {
  if (!confirm('Delete the saved calibration?')) return;
  fetch('/delete_calibration', { method: 'POST' }).then(() => location.reload());
}

/* ------------------------------------------------------------------- sync */
function openSampleModal() {
  $('sampleModal').style.display = 'flex';
  if (!sampleRows) addSampleRow();
}
function closeModals() {
  document.querySelectorAll('.modal-overlay').forEach((m) => (m.style.display = 'none'));
}
function addSampleRow() {
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${sampleRows + 1}</td>
    <td><input type="file" accept=".csv" id="sDiy_${sampleRows}"></td>
    <td><input type="file" accept=".csv,.xls,.xlsx" id="sCom_${sampleRows}"></td>`;
  document.querySelector('#sampleTable tbody').appendChild(tr);
  sampleRows++;
}
async function submitSamples() {
  const fd = new FormData();
  let n = 0;
  for (let i = 0; i < sampleRows; i++) {
    const d = $(`sDiy_${i}`).files[0], c = $(`sCom_${i}`).files[0];
    if (d && c) { fd.append(`diy_${n}`, d); fd.append(`com_${n}`, c); n++; }
  }
  if (n < 2) { alert('At least two complete pairs are needed.'); return; }

  const res = await fetch('/calibrate_samples', { method: 'POST', body: fd });
  const data = await res.json().catch(() => ({}));
  if (data.status === 'success') {
    let msg = `Calibrated with ${data.count} points (degree ${data.degree}).`;
    if (data.skipped && data.skipped.length) msg += `\nSkipped: ${data.skipped.join(', ')}`;
    alert(msg);
    location.reload();
  } else {
    let msg = 'Could not sync: ' + (data.message || res.status);
    if (data.skipped && data.skipped.length) msg += `\nSkipped: ${data.skipped.join(', ')}`;
    alert(msg);
  }
}

/* ----------------------------------------------------------------- camera */
function bindControl(rangeId, numId, control) {
  const range = $(rangeId), num = $(numId);
  let timer = null;
  const send = (v) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      fetch('/set_control', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ control, value: v })
      }).catch(() => {});
    }, 120);
  };
  range.addEventListener('input', () => { num.value = range.value; send(range.value); });
  num.addEventListener('change', () => {
    const v = Math.min(Number(range.max), Math.max(Number(range.min), Number(num.value)));
    num.value = v; range.value = v; send(v);
  });
}

/* Smoothing is purely client-side display state, so it needs no server round
   trip — just keep the slider and number input in sync and force the next
   redraw to use the new window. */
function bindSmoothing(rangeId, numId) {
  const range = $(rangeId), num = $(numId);
  const apply = (v) => {
    let n = Math.round(Number(v));
    if (n % 2 === 0) n += 1; // keep the window odd so it stays centered
    n = Math.min(Number(range.max), Math.max(Number(range.min), n));
    smoothWindow = n;
    range.value = n; num.value = n;
  };
  range.addEventListener('input', () => apply(range.value));
  num.addEventListener('change', () => apply(num.value));
}

/* -------------------------------------------------------------------- init */
window.addEventListener('resize', drawROI);
$('videoFeed').addEventListener('load', drawROI);
$('mainChart').addEventListener('mouseleave', () => { $('readout').textContent = '—'; });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModals(); });
$('sampleModal').addEventListener('click', (e) => {
  if (e.target.id === 'sampleModal') closeModals();
});

bindControl('expRange', 'expNum', 'exposure');
bindControl('gainRange', 'gainNum', 'gain');
bindSmoothing('smoothRange', 'smoothNum');
initChart();
updateManualUI();
loop();
