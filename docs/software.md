# The software

MonoSpectro runs as a small Flask server on the Raspberry Pi and is driven entirely from
a browser on the same network. Nothing is installed on the client — you point a laptop,
tablet or phone at the Pi and you are looking at the instrument.

<p align="center">
  <img src="images/ui_main.png" width="820" alt="MonoSpectro web interface">
</p>

<p align="center"><i>The whole instrument is this one screen.</i></p>

---

## How it is put together

```mermaid
flowchart TB
    subgraph pi["Raspberry Pi — app.py"]
        CAM[picamera2] --> GRAY[grayscale frame]
        GRAY --> MJPEG["/video_feed<br/>MJPEG stream"]
        GRAY --> ROI["ROI row-average<br/>one value per pixel column"]
        ROI --> SPEC["/spectrum<br/>JSON, 5x per second"]
        CAL["calibration.json<br/>pixel to nm polynomial"] --> SPEC
    end
    subgraph browser["Browser — script.js"]
        MJPEG --> VIDEO[live image + ROI box]
        SPEC --> CHART[Chart.js line chart]
        CHART --> CSV[CSV download]
    end
```

The Pi holds exactly one piece of state that survives a restart: `calibration.json`, the
pixel-to-nanometre polynomial. Everything else — the blank reference, the selected ROI,
the calibration points you are in the middle of picking — lives in the browser tab and
is gone when you reload. That is worth knowing before you close it.

The browser polls `/spectrum` every 200 ms and redraws. The camera image arrives
separately as an MJPEG stream, so the video and the chart are independent: if the chart
freezes, the stream will usually keep running, and vice versa.

---

## The screen, region by region

### The camera view

<p align="center">
  <img src="images/ui_roi.png" width="620" alt="Camera view with the ROI box over the spectral line">
</p>

The live grayscale image from the OV9281. The dispersed spectrum appears as a bright
horizontal streak. The green rectangle is the **ROI** — the only part of the sensor that
becomes data.

Every column of pixels inside the rectangle is averaged vertically, producing one
intensity value per column. Averaging is what buys the signal-to-noise: a 120-pixel-tall
ROI averages 120 samples per wavelength.

Set it with the two text boxes, as `x;y` for the top-left corner and `x;y` for the
bottom-right, then press **ROI**:

| Box | Meaning | Default |
| --- | --- | --- |
| first | `x1;y1` — top-left corner | `100;300` |
| second | `x2;y2` — bottom-right corner | `1100;420` |

Make the box hug the streak. Too tall and you average in dark rows, which drags the
whole spectrum down and flattens your peaks. Too short and you throw away signal.

> The ROI is **not** saved. On reload it returns to the default, and so does the
> wavelength mapping that depends on where `x1` and `x2` sit — the pixel axis is built
> with `linspace(x1, x2, n)`, so moving the ROI moves the wavelength assigned to every
> point. Re-set the ROI *before* taking a blank, never after.

### The chart

The green trace is the live spectrum: wavelength on X, raw intensity 0–255 on Y. Red
dots are calibration points you have picked.

**Y saturates at 255.** The sensor is 8 bits, so a peak sitting flat at 255 is clipped
and its true height is unknown — and any absorbance computed from it is wrong. Use
exposure and gain to put the brightest part of your *blank* just below the ceiling.

### The sidebar

**Exp** and **Gain** are sent straight to the camera via `picamera2`:

| Control | What it does | Cost of turning it up |
| --- | --- | --- |
| Exp | exposure time, in milliseconds | Slower refresh; motion blur is irrelevant here, so this is the one to raise first |
| Gain | analogue gain | Amplifies signal *and* noise — raise only after exposure is exhausted |

**Calibración Manual** lists the calibration points you have picked, with **Calcular** to
fit them and **Limpiar** to start over.

---

## Taking a measurement

The order matters. The blank has to be taken under exactly the conditions the sample
will be measured in — same exposure, same gain, same ROI.

1. **Frame it.** Put the blank in the beam, set the ROI over the streak, adjust exposure
   and gain until the brightest point sits just under 255.
2. **Calibrate the wavelength axis**, if you have not already. Both methods are below.
3. **Blanco.** With the solvent-only cuvette in place, press it. The current spectrum is
   stored in memory as *I₀*.
4. **Guardar Abs.** Swap in the sample and press it. Your browser downloads a CSV.

Absorbance is computed in the browser, per point:

```js
A = log10( max(I0, 1) / max(I, 1) )
```

The `max(…, 1)` guards against a division by zero on dead pixels. It also means a truly
black pixel reports `log10(I0)` rather than infinity — a ceiling, not an error.

The file is named `abs_<timestamp>.csv` and lands in your browser's download folder,
**not on the Pi**. Rename it to something meaningful straight away; a folder of
`abs_1743363021456.csv` is unusable a month later.

```csv
Pixel,Wavelength_nm,abs
100.00,401.23,0.0389
101.00,402.31,0.0512
```

> **Guardar Abs refuses to run without a blank.** That is deliberate — there is no
> sensible absorbance without a reference.

---

## Calibrating the wavelength axis

Out of the box the X axis is a guess: a straight line from 350 to 750 nm across the ROI.
It is a placeholder, not a measurement. Both methods below replace it with a polynomial
fitted from real reference points, saved to `calibration.json` and reloaded on every
start.

### Method 1 — known emission lines

<p align="center">
  <img src="images/ui_calibration.png" width="620" alt="Calibration mode with points picked on the chart">
</p>

1. Shine a source with known lines through the instrument. A compact fluorescent lamp is
   ideal — its mercury lines at **436, 546 and 611 nm** are sharp and unmistakable.
2. Press **Calibrar Picos**. The chart border turns pink and the button reads
   `CLICK EN PICOS`.
3. Click a peak. A prompt shows the pixel position and asks for the wavelength in nm.
4. Repeat for every line you can identify. They appear in the sidebar as `pixel -> nm`.
5. Press **Calcular**.

The fit degree adapts to how much you gave it:

| Points | Fit |
| --- | --- |
| 2–3 | 1st degree — a straight line |
| 4 or more | 2nd degree — a parabola |

Two points is the minimum and it is not enough: a straight line cannot describe a
grating (see *Known deviations* in the main README). Three mercury lines and a laser
pointer is a much better afternoon's work.

The page reloads when the fit is saved.

### Method 2 — sync against a reference instrument

Press **Sincronizar** to open the pairing dialog. Each row takes two files for the same
sample: your own exported CSV, and that sample measured on a reference
spectrophotometer.

For each pair the server:

1. Smooths both curves with a 25-point centred rolling mean — raw peaks are too noisy to
   trust a single maximum.
2. Takes the position of the maximum in each: a **pixel** from yours, a **nanometre**
   from theirs.
3. Averages the pixel values of any samples that report the same reference wavelength,
   so two dyes peaking at the same place cannot pull the fit in two directions at once.
4. Fits the collected `(pixel, nm)` pairs.

| Pairs | Fit |
| --- | --- |
| fewer than 6 | 1st degree — forced, to stop the axis exploding |
| 6 or more | 2nd degree |

A sanity check then evaluates the fit at pixel 600 and rejects it if the result falls
outside 200–1100 nm, which catches the degenerate fits that would otherwise silently
produce a nonsense axis.

The commercial file is read with `skiprows=7`, wavelength from column 0 and absorbance
from column 2. If your instrument exports a different shape, that is the line to change.

> **Pick samples whose peaks are spread out.** Eleven dyes that all absorb between 490
> and 500 nm give you one calibration point, not eleven. Aim to cover 420–700 nm.

---

## Running it as a field instrument

The Pi can be its own access point, so the instrument needs no infrastructure — power it
up on a boat or a bench and connect to it directly.

```bash
sudo nmcli con add type wifi ifname wlan0 con-name MonoSpectro autoconnect yes \
  ssid MonoSpectro
sudo nmcli con modify MonoSpectro 802-11-wireless.mode ap \
  802-11-wireless.band bg ipv4.method shared
sudo nmcli con modify MonoSpectro wifi-sec.key-mgmt wpa-psk
sudo nmcli con modify MonoSpectro wifi-sec.psk "12345678"
sudo nmcli con up MonoSpectro
```

Join the `MonoSpectro` network and the interface is at **http://10.42.0.1:5000**.

> **With no internet, a CDN is a single point of failure.** `templates/index.html`
> currently pulls Chart.js from jsDelivr. In access-point mode that request goes nowhere
> and the chart never appears. Download `chart.umd.js` into `static/chart.min.js` while
> the Pi still has internet, and point the tag at the local copy.

To start on boot, `/etc/systemd/system/monospectro.service`:

```ini
[Unit]
Description=MonoSpectro web server
After=network.target

[Service]
User=alex
WorkingDirectory=/home/alex/spectro_web
ExecStart=/home/alex/spectro_web/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now monospectro.service
journalctl -u monospectro.service -f     # logs
```

---

## HTTP endpoints

Useful if you want to script the instrument instead of clicking it.

| Method | Route | Body | Returns |
| --- | --- | --- | --- |
| GET | `/` | — | the interface |
| GET | `/video_feed` | — | MJPEG stream, grayscale, JPEG quality 70 |
| POST | `/spectrum` | `{"selection": {x1,y1,x2,y2}}` | `{wavelengths, intensities, pixels}` |
| POST | `/calibrate_manual` | `{"points": [{pixel, wavelength}, …]}` | `{"status": "success"}` |
| POST | `/calibrate_samples` | multipart: `diy_0`, `com_0`, `diy_1`, … | `{"status", "count"}` |
| POST | `/set_control` | `{"control": "exposure"\|"gain", "value": n}` | `{"status": "success"}` |

`/spectrum` doubles as the ROI setter — posting a `selection` updates it for the video
thread too. There is no authentication of any kind; this is a local-network instrument,
not a service to put on the internet.

---

## When something looks wrong

| Symptom | Likely cause |
| --- | --- |
| Chart empty, video fine | Chart.js did not load — CDN unreachable in AP mode |
| Video black | Camera busy or not detected. `journalctl -u monospectro -f`, check for `ov9281` |
| Flat trace pinned at 255 | Saturated. Drop exposure, then gain |
| Peaks in the right place, absorbance too low | ROI includes dark rows above or below the streak |
| Absorbance negative | Blank was dimmer than the sample — usually the blank was taken at a different exposure |
| Wavelengths wildly wrong after Sincronizar | Too few pairs, or several samples peaking at the same wavelength |
| ROI resets on reload | Expected — it is not persisted |
