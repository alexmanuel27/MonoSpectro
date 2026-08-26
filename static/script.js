let currentW = [], currentI = [], currentP = [], refI = null, roi = {x1:100, y1:300, x2:1100, y2:420}, chart;
let isCalMode = false, manualPoints = [], samCount = 0;

function initChart() {
    const ctx = document.getElementById('mainChart').getContext('2d');
    chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                { label: 'Espectro', data: [], borderColor: '#10b981', pointRadius: 0, fill: false, order: 2 },
                { label: 'Puntos', data: [], backgroundColor: '#ff4444', pointRadius: 6, type: 'scatter', order: 1 }
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            scales: { x: { ticks: { color: '#888' } } },
            onClick: (e) => {
                if (!isCalMode) return;
                const points = chart.getElementsAtEventForMode(e, 'index', { intersect: false }, true);
                if (points.length > 0) {
                    const idx = points[0].index;
                    const wl = prompt(`Píxel: ${currentP[idx].toFixed(1)}\n¿nm?`);
                    if (wl && !isNaN(wl)) {
                        manualPoints.push({ pixel: currentP[idx], wavelength: wl, chartX: currentW[idx].toFixed(1), chartY: currentI[idx] });
                        updateManualUI();
                    }
                }
            }
        }
    });
}

async function loop() {
    try {
        const res = await fetch('/spectrum', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ selection: roi }) });
        const data = await res.json();
        currentW = data.wavelengths; currentI = data.intensities; currentP = data.pixels;
        chart.data.labels = currentW.map(w => w.toFixed(1));
        chart.data.datasets[0].data = currentI;
        chart.data.datasets[1].data = manualPoints.map(p => ({ x: p.chartX, y: p.chartY }));
        chart.update();
        drawROI();
    } catch(e){}
    setTimeout(loop, 200);
}

function drawROI() {
    const img = document.getElementById('videoFeed'), box = document.getElementById('roiBox');
    if (!img || !img.clientWidth) return;
    const scale = img.clientWidth / 1280;
    box.style.left = (roi.x1 * scale) + "px"; box.style.top = (roi.y1 * scale) + "px";
    box.style.width = ((roi.x2 - roi.x1) * scale) + "px"; box.style.height = ((roi.y2 - roi.y1) * scale) + "px";
}

function updateROI() {
    const p1 = document.getElementById('roi1').value.split(';');
    const p2 = document.getElementById('roi2').value.split(';');
    roi = { x1: parseInt(p1[0]), y1: parseInt(p1[1]), x2: parseInt(p2[0]), y2: parseInt(p2[1]) };
}

function toggleCalMode() {
    isCalMode = !isCalMode;
    document.getElementById('chartContainer').classList.toggle('cal-active', isCalMode);
    document.getElementById('calBtn').innerText = isCalMode ? "CLICK EN PICOS" : "Calibrar Picos (OFF)";
}

function updateManualUI() {
    document.getElementById('puntosLista').innerHTML = manualPoints.map(p => `<li>${p.pixel.toFixed(1)} -> ${p.wavelength}</li>`).join('');
    document.getElementById('btnSubmitCal').style.display = manualPoints.length >= 2 ? "block" : "none";
}

function clearPoints() { manualPoints = []; updateManualUI(); }

async function submitManualCal() {
    const res = await fetch('/calibrate_manual', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ points: manualPoints }) });
    if ((await res.json()).status === 'success') location.reload();
}

function takeRef() { refI = [...currentI]; alert("Blanco OK"); }

function saveAbs() {
    if(!refI) return alert("Falta Blanco");
    let csv = "Pixel,Wavelength_nm,abs\n";
    currentI.forEach((v, i) => { csv += `${currentP[i].toFixed(2)},${currentW[i].toFixed(2)},${Math.log10(Math.max(refI[i],1)/Math.max(v,1)).toFixed(4)}\n`; });
    const b = new Blob([csv], {type:'text/csv'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download=`abs_${Date.now()}.csv`; a.click();
}

function openSampleModal() { document.getElementById('sampleModal').style.display='flex'; if(samCount==0) addSampleRow(); }
function closeModals() { document.querySelectorAll('.modal-overlay').forEach(m => m.style.display='none'); }
function addSampleRow() {
    const b = document.querySelector('#sampleTable tbody');
    const r = document.createElement('tr');
    r.innerHTML = `<td>${samCount+1}</td><td><input type="file" id="sDiy_${samCount}"></td><td><input type="file" id="sCom_${samCount}"></td>`;
    b.appendChild(r); samCount++;
}
async function submitSamples() {
    const fd = new FormData(); let c = 0;
    for(let i=0; i<samCount; i++) {
        const d = document.getElementById(`sDiy_${i}`).files[0], m = document.getElementById(`sCom_${i}`).files[0];
        if(d && m) { fd.append(`diy_${c}`, d); fd.append(`com_${c}`, m); c++; }
    }
    const res = await fetch('/calibrate_samples', { method: 'POST', body: fd });
    const data = await res.json();
    if(data.status === 'success') { alert("Sincronización Exitosa."); location.reload(); }
    else { alert("Error: " + data.message); }
}

function setCam(control, value) { fetch('/set_control', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ control, value }) }); }

initChart(); loop();
