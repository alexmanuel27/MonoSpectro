"""MonoSpectro — Flask server for the camera spectrometer.

Streams the monochrome sensor, extracts a spectrum from a region of interest,
maps pixel position to wavelength, and serves a browser interface on port 5000.
"""

import json
import os
import threading
import time

import cv2
import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, render_template, request

try:
    from picamera2 import Picamera2
except ImportError:                                   # dev machine without the Pi stack
    Picamera2 = None

app = Flask(__name__)

FRAME_W, FRAME_H = 1280, 720
ROI_FILE, CAL_FILE = "roi.json", "calibration.json"
DEFAULT_ROI = {"x1": 100, "y1": 300, "x2": 1100, "y2": 420}

# Wavelengths assumed when nothing has been calibrated yet. A placeholder, not a
# measurement — the interface says so.
FALLBACK_MIN_NM, FALLBACK_MAX_NM = 350.0, 750.0

# A fit is rejected if it maps the middle of the sensor outside this band.
SANITY_MIN_NM, SANITY_MAX_NM = 200.0, 1100.0

_state_lock = threading.Lock()
latest_spectrum: list = []
calibration_coeffs = None
selection = dict(DEFAULT_ROI)
camera_error = None


# --------------------------------------------------------------------- state
def sanitise_roi(raw):
    """Clamp a client-supplied ROI to the sensor, or raise ValueError."""
    try:
        x1, y1, x2, y2 = (int(raw[k]) for k in ("x1", "y1", "x2", "y2"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("ROI needs integer x1, y1, x2 and y2")

    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    x1, x2 = max(0, x1), min(FRAME_W, x2)
    y1, y2 = max(0, y1), min(FRAME_H, y2)
    if x2 - x1 < 2 or y2 - y1 < 2:
        raise ValueError("ROI must be at least 2x2 pixels inside the frame")
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def load_settings():
    global calibration_coeffs, selection
    if os.path.exists(CAL_FILE):
        try:
            with open(CAL_FILE) as f:
                coeffs = json.load(f).get("coeffs")
            calibration_coeffs = list(coeffs) if coeffs else None
        except (OSError, ValueError, TypeError):
            calibration_coeffs = None
    if os.path.exists(ROI_FILE):
        try:
            with open(ROI_FILE) as f:
                selection = sanitise_roi(json.load(f))
        except (OSError, ValueError, TypeError):
            selection = dict(DEFAULT_ROI)


def save_json(path, payload):
    """Write atomically so a power cut cannot leave a truncated settings file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


load_settings()


# -------------------------------------------------------------------- camera
picam2 = None
if Picamera2 is not None:
    try:
        picam2 = Picamera2()
        picam2.configure(picam2.create_still_configuration(main={"size": (FRAME_W, FRAME_H)}))
        picam2.start()
    except Exception as exc:                          # noqa: BLE001 - reported to the UI
        camera_error = f"{type(exc).__name__}: {exc}"
        picam2 = None
        print(f"Camera unavailable — {camera_error}")
else:
    camera_error = "picamera2 is not installed"
    print("Camera unavailable — picamera2 is not installed")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/video_feed")
def video_feed():
    def gen():
        global latest_spectrum
        while True:
            if picam2 is None:
                time.sleep(1.0)
                continue
            try:
                frame = picam2.capture_array()
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY) if frame.ndim == 3 else frame

                with _state_lock:
                    roi = dict(selection)             # one consistent snapshot
                band = gray[roi["y1"]:roi["y2"], roi["x1"]:roi["x2"]]
                if band.size:
                    latest_spectrum = np.mean(band, axis=0).astype(int).tolist()

                ok, buf = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, 50])
                if ok:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + buf.tobytes() + b"\r\n")
                time.sleep(0.04)
            except GeneratorExit:
                break
            except Exception as exc:                  # noqa: BLE001
                # Sleep before retrying: without this a persistent failure spins
                # the CPU at 100 % for as long as the page is open.
                print(f"Frame error — {type(exc).__name__}: {exc}")
                time.sleep(0.5)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/spectrum", methods=["POST"])
def get_spectrum():
    global selection
    data = request.get_json(silent=True) or {}

    if "selection" in data:
        try:
            new_roi = sanitise_roi(data["selection"])
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        with _state_lock:
            changed = new_roi != selection
            selection = new_roi
        # Only touch the SD card when the value actually changed. The browser
        # posts its ROI five times a second; writing every time wears the card
        # out for nothing.
        if changed:
            save_json(ROI_FILE, new_roi)

    spectrum = list(latest_spectrum)
    with _state_lock:
        roi, coeffs = dict(selection), calibration_coeffs

    n = len(spectrum)
    pixels = np.linspace(roi["x1"], roi["x2"], n) if n else np.array([])
    if coeffs and n:
        wavelengths = np.polyval(coeffs, pixels).tolist()
    else:
        wavelengths = np.linspace(FALLBACK_MIN_NM, FALLBACK_MAX_NM, n).tolist() if n else []

    return jsonify({
        "wavelengths": wavelengths,
        "intensities": spectrum,
        "pixels": pixels.tolist(),
        "is_calibrated": coeffs is not None,
        "coeffs": coeffs,
        "roi": roi,
        "camera_error": camera_error,
    })


def store_fit(coeffs):
    """Reject a fit that puts the middle of the sensor somewhere impossible."""
    global calibration_coeffs
    probe = float(np.polyval(coeffs, FRAME_W / 2))
    if not np.isfinite(probe) or not (SANITY_MIN_NM <= probe <= SANITY_MAX_NM):
        return False, f"unstable fit — pixel {FRAME_W // 2} maps to {probe:.0f} nm"
    with _state_lock:
        calibration_coeffs = list(coeffs)
    save_json(CAL_FILE, {"coeffs": list(coeffs)})
    return True, None


@app.route("/delete_calibration", methods=["POST"])
def delete_calibration():
    global calibration_coeffs
    if os.path.exists(CAL_FILE):
        os.remove(CAL_FILE)
    with _state_lock:
        calibration_coeffs = None
    return jsonify({"status": "success"})


@app.route("/calibrate_manual", methods=["POST"])
def calibrate_manual():
    data = request.get_json(silent=True) or {}
    points = data.get("points") or []
    try:
        px = [float(p["pixel"]) for p in points]
        wl = [float(p["wavelength"]) for p in points]
    except (KeyError, TypeError, ValueError):
        return jsonify({"status": "error", "message": "Malformed calibration points"}), 400

    if len(px) < 2:
        return jsonify({"status": "error", "message": "At least two points are needed"}), 400
    if len(set(px)) < 2:
        return jsonify({"status": "error",
                        "message": "All the points sit on the same pixel"}), 400

    degree = 2 if len(px) >= 4 else 1
    coeffs = np.polyfit(px, wl, degree).tolist()
    ok, err = store_fit(coeffs)
    if not ok:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify({"status": "success", "coeffs": coeffs, "degree": degree})


@app.route("/calibrate_samples", methods=["POST"])
def calibrate_samples():
    """Pair each of our spectra with the same sample measured on a reference
    instrument, and fit pixel -> nm from where the two peaks land."""
    files = request.files
    raw_points, skipped = {}, []

    for key in [k for k in files if k.startswith("diy_")]:
        idx = key.split("_", 1)[1]
        try:
            df_diy = pd.read_csv(files[key])
            smoothed = df_diy["abs"].rolling(window=31, center=True).mean()
            pixel = float(df_diy.loc[smoothed.idxmax(), "Pixel"])

            com = files.get(f"com_{idx}")
            if com is None:
                skipped.append(f"{idx}: no reference file")
                continue
            name = (com.filename or "").lower()
            df_com = (pd.read_excel(com, skiprows=7) if name.endswith((".xls", ".xlsx"))
                      else pd.read_csv(com, skiprows=7))
            com_smoothed = df_com.iloc[:, 2].rolling(window=31, center=True).mean()
            wl = round(float(df_com.iloc[com_smoothed.idxmax(), 0]), 1)
        except Exception as exc:                      # noqa: BLE001
            skipped.append(f"{idx}: {type(exc).__name__}")
            continue
        raw_points.setdefault(wl, []).append(pixel)

    # Two samples peaking at the same wavelength would otherwise pull the fit in
    # two directions at once; average their pixel positions instead.
    pairs = sorted((float(np.mean(v)), wl) for wl, v in raw_points.items())
    if len(pairs) < 2:
        return jsonify({"status": "error",
                        "message": "Fewer than two usable pairs",
                        "skipped": skipped}), 400

    px, wl = zip(*pairs)
    degree = 2 if len(pairs) >= 6 else 1
    coeffs = np.polyfit(px, wl, degree).tolist()
    ok, err = store_fit(coeffs)
    if not ok:
        return jsonify({"status": "error", "message": err, "skipped": skipped}), 400
    return jsonify({"status": "success", "count": len(pairs),
                    "degree": degree, "coeffs": coeffs, "skipped": skipped})


@app.route("/set_control", methods=["POST"])
def set_control():
    if picam2 is None:
        return jsonify({"status": "error", "message": "No camera"}), 503
    d = request.get_json(silent=True) or {}
    control, value = d.get("control"), d.get("value")
    try:
        if control == "exposure":
            picam2.set_controls({"ExposureTime": int(value) * 1000})
        elif control == "gain":
            picam2.set_controls({"AnalogueGain": float(value)})
        else:
            return jsonify({"status": "error", "message": f"Unknown control {control}"}), 400
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
