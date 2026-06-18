# Automated Behavioural Monitoring in Remote Testing Environment

This project monitors student behaviour during online examinations using **Python**, **Flask**, **OpenCV**, **MediaPipe**, **SQLite**, and **basic browser events**.

It automatically flags suspicious activities such as:

- Looking away from the screen / head turned for several seconds
- Face missing from the frame
- Multiple faces in front of the camera
- Loud background noise near the microphone
- Tab switching / exam tab losing focus / closing the tab

All suspicious events are stored in a local SQLite database and can be reviewed in a simple web-based report.

---

## 1. Requirements

- Python 3.9 or newer
- A webcam
- A microphone
- Windows 10+ (your current environment)

Install Python dependencies from the project root:

```bash
pip install -r requirements.txt
```

On Windows, `sounddevice` may require additional system audio libraries. If installation fails, you can comment out the `AudioMonitor` usage in `app.py` to run video-only monitoring.

---

## 2. Running the monitoring server

From the project root (`d:\exam_monitoring_main`):

```bash
python app.py
```

Then open your browser and visit:

- Exam monitoring page: `http://127.0.0.1:5000/`
- Suspicious activity report: `http://127.0.0.1:5000/report`

When you open the exam page, allow camera and microphone access when prompted.

---

## 3. How it works (high level)

- `app.py`  
  Runs the Flask web server, streams annotated webcam video, receives client events (tab switch, visibility changes, etc.), and exposes a simple report endpoint.

- `video_analyzer.py`  
  Uses MediaPipe face detection to:
  - detect no face / multiple faces
  - estimate whether the face is centered in the frame  
  If the face is missing, there are multiple faces, or the face is away from the center for several seconds, it logs a suspicious event.

- `audio_monitor.py`  
  Runs in a background thread, sampling the microphone and computing RMS loudness. If the environment is consistently loud, it logs a `loud_noise` event.

- `static/js/monitor.js`  
  Runs in the browser, listening for:
  - `blur` / `focus` events
  - `visibilitychange`
  - `beforeunload`  
  and sends them back to the server as suspicious client events (e.g. tab hidden, tab closed, window lost focus).

- `monitoring_db.py`  
  Wraps a local SQLite database (`monitoring.db`) with helper functions to initialize the schema, insert events, and fetch recent events for reporting.

---

## 4. Notes and possible enhancements

- The current heuristics are intentionally simple and interpretable (time-based thresholds and face position in the frame).
- You can tune thresholds (e.g. how many seconds before an event is raised) by editing the constructor parameters in `BehaviourAnalyzer` and `AudioMonitor`.
- Future extensions could include:
  - mobile phone detection
  - emotion recognition
  - an overall AI-based cheating score
  - per-student/session identifiers instead of a single shared log

