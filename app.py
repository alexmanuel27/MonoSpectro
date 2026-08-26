import cv2
import numpy as np
import json
import os
import pandas as pd
from flask import Flask, render_template, Response, jsonify, request
from picamera2 import Picamera2

app = Flask(__name__)

latest_spectrum = []
calibration_coeffs = None
selection = {'x1': 100, 'y1': 300, 'x2': 1100, 'y2': 420}

def load_calibration():
    global calibration_coeffs
    if os.path.exists('calibration.json'):
        try:
            with open('calibration.json', 'r') as f:
                data = json.load(f); calibration_coeffs = data.get('coeffs')
        except: calibration_coeffs = None

load_calibration()

try:
    picam2 = Picamera2()
    config = picam2.create_still_configuration(main={"size": (1280, 720)})
    picam2.configure(config)
    picam2.start()
except: print("Error de cámara detectado")

@app.route('/')
def index(): return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    def gen():
        while True:
            try:
                frame = picam2.capture_array()
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY) if len(frame.shape)==3 else frame
                global latest_spectrum
                x1, y1, x2, y2 = [int(selection[k]) for k in ['x1', 'y1', 'x2', 'y2']]
                roi = gray[y1:y2, x1:x2]
                if roi.size > 0: latest_spectrum = np.mean(roi, axis=0).astype(int).tolist()
                ret, buffer = cv2.imencode('.jpg', gray, [cv2.IMWRITE_JPEG_QUALITY, 70])
                yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            except GeneratorExit: break
            except: continue
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/spectrum', methods=['POST'])
def get_spectrum():
    global selection
    data = request.get_json() or {}
    if 'selection' in data: selection = data['selection']
    num_points = len(latest_spectrum)
    x_pixels = np.linspace(selection['x1'], selection['x2'], num_points)
    
    if calibration_coeffs and len(calibration_coeffs) > 0:
        wavelengths = np.polyval(calibration_coeffs, x_pixels).tolist()
    else:
        wavelengths = np.linspace(350, 750, num_points).tolist()
    return jsonify({"wavelengths": wavelengths, "intensities": latest_spectrum, "pixels": x_pixels.tolist()})

@app.route('/calibrate_manual', methods=['POST'])
def calibrate_manual():
    global calibration_coeffs
    data = request.get_json()
    px = [float(p['pixel']) for p in data['points']]
    wl = [float(p['wavelength']) for p in data['points']]
    calibration_coeffs = np.polyfit(px, wl, 1 if len(px) < 4 else 2).tolist()
    with open('calibration.json', 'w') as f:
        json.dump({"coeffs": calibration_coeffs}, f)
    return jsonify({"status": "success"})

@app.route('/calibrate_samples', methods=['POST'])
def calibrate_samples():
    global calibration_coeffs
    files = request.files
    pair_keys = [k for k in files.keys() if k.startswith('diy_')]
    raw_points = {}

    for key in pair_keys:
        idx = key.split('_')[1]
        try:
            # Procesar DIY
            df_diy = pd.read_csv(files.get(f'diy_{idx}'))
            # Usar ventana de 25 para suavizar mucho el ruido
            diy_abs = df_diy['abs'].rolling(window=25, center=True).mean()
            idx_p = diy_abs.idxmax()
            pixel_val = df_diy.loc[idx_p, 'Pixel']

            # Procesar Comercial
            com_file = files.get(f'com_{idx}')
            if com_file.filename.lower().endswith(('.xls', '.xlsx')):
                df_com = pd.read_excel(com_file, skiprows=7)
            else:
                df_com = pd.read_csv(com_file, skiprows=7)
            
            # Columna 0: Wave, Columna 2: Absorbancia
            com_abs = df_com.iloc[:, 2].rolling(window=25, center=True).mean()
            wl_val = df_com.iloc[com_abs.idxmax(), 0]
            
            if wl_val not in raw_points: raw_points[wl_val] = []
            raw_points[wl_val].append(pixel_val)
        except: continue

    # Promediar píxeles si hay colisiones en la misma WL
    final_pts = []
    for wl, pixels in raw_points.items():
        final_pts.append((np.mean(pixels), wl))
    
    if len(final_pts) < 2:
        return jsonify({"status": "error", "message": "No hay suficientes puntos válidos"}), 400

    final_pts.sort()
    px_f, wl_f = zip(*final_pts)
    
    # FORZAR GRADO 1 si hay menos de 6 muestras para evitar que la escala explote
    degree = 2 if len(final_pts) >= 6 else 1
    new_coeffs = np.polyfit(px_f, wl_f, degree).tolist()
    
    # Test de cordura ultra-flexible (200nm a 1100nm)
    test = np.polyval(new_coeffs, 600)
    if test < 200 or test > 1100:
        return jsonify({"status": "error", "message": "Calibración fallida: Curva matemática inestable"}), 400

    calibration_coeffs = new_coeffs
    with open('calibration.json', 'w') as f:
        json.dump({"coeffs": calibration_coeffs}, f)
    return jsonify({"status": "success", "count": len(final_pts)})

@app.route('/set_control', methods=['POST'])
def set_control():
    d = request.get_json()
    if d.get('control') == 'exposure': picam2.set_controls({"ExposureTime": int(d['value']) * 1000})
    elif d.get('control') == 'gain': picam2.set_controls({"AnalogueGain": float(d['value'])})
    return jsonify({"status": "success"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
