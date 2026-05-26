# DroidCam MVP - Wireless Local Camera Streamer

A lightweight browser-based webcam MVP built with Python Flask, Flask-SocketIO, and Tailwind CSS. Scan a dynamically generated QR code on your PC dashboard with your phone to instantly turn the phone into a low-latency wireless webcam streaming over your local network (WiFi).

## Features

-  **Dynamic QR Pairing**: Automatic local IP detection generates a unique QR code on the PC dashboard. Scanning it opens the mobile camera page immediately.
-  **Real-Time Low Latency Streaming**: Frames captured via HTML5 `getUserMedia`, scaled and compressed via HTML5 `<canvas>`, and streamed over WebSockets (`Socket.IO`).
-  **Dual Camera Switch**: Seamless toggle between front and back camera layouts on the phone.
-  **Bidirectional Play/Pause Controls**: Start/Stop streaming directly from the phone or trigger it remotely from the PC dashboard.
-  **Beautiful Dark UI**: High-fidelity theme featuring glassmorphic designs, connection status dots, and an live FPS / bandwidth counter.

---

## Tech Stack

- **Backend**: Python 3, Flask, Flask-SocketIO, Eventlet (for high-performance WebSocket networking)
- **Frontend**: HTML5, Vanilla JavaScript, Tailwind CSS v3 (via CDN), custom CSS animations
- **Libraries**: `qrcode` (QR Generation), `pyOpenSSL` (Self-signed dynamic certificates)

---

## Quick Start

### 1. Prerequisites
- Python 3.8+ installed on the host PC.
- A smartphone connected to the **same local network/WiFi router** as your PC.

### 2. Installation
Navigate to the project root directory and install dependencies:

```bash
pip install -r requirements.txt
```

### 3. Running the Server
Launch the application:

```bash
python app.py
```

The server automatically detects your LAN IP, generates SSL certificates, and launches the web application.

---

## Connection Guide & HTTPS Requirement

Mobile browsers enforce a security restriction where **camera access is only permitted in "Secure Contexts" (HTTPS)**. To facilitate local camera streaming without complex certificate setups, the application runs on **local HTTPS** using a dynamically generated self-signed certificate.

### Step-by-step Connection:

1. **Open the Dashboard**:
   Go to `https://localhost:5000` on your PC web browser.
   - *Note*: Your browser will show a warning saying "Your connection is not private" or "Potential Security Risk Ahead". This is normal for self-signed certificates. Click **Advanced** and then **Proceed to localhost (unsafe)**.

2. **Scan the QR Code**:
   Scan the displayed QR code with your phone camera or enter the mobile URL manually in your phone's browser (e.g. `https://192.168.1.50:5000/mobile`).

3. **Bypass Certificate Warning on Phone**:
   Just like on the PC, your phone browser will show a security warning because of the self-signed certificate.
   - Click **Advanced** or **More Details** and select **Proceed / Continue** to load the page.

4. **Grant Camera Permissions**:
   Once the page loads, allow browser access to your phone's camera.

5. **Start Streaming**:
   Press **Start Streaming** on your phone! The live camera feed will instantly render in real-time on your PC dashboard, with stats updating dynamically.

---

## Project Structure

```
project/
├── app.py                # Main backend server
├── requirements.txt      # Python dependencies
├── static/
│   └── style.css         # Custom animations & glassmorphic styling
└── templates/
    ├── dashboard.html    # PC browser display page
    └── mobile.html       # Mobile phone capture page
```

## Author 
Qamber Muhammad Hanif 
