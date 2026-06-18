from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, List, Optional, Tuple

import cv2

from monitoring_db import log_event


face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
eye_glasses_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml"
)


@dataclass
class FaceEventState:
    last_no_face_time: Optional[datetime] = None
    last_multiple_faces_time: Optional[datetime] = None
    last_look_away_time: Optional[datetime] = None
    last_eye_missing_time: Optional[datetime] = None
    last_eye_away_time: Optional[datetime] = None
    ongoing_warnings: Dict[str, str] = None

    def __post_init__(self) -> None:
        if self.ongoing_warnings is None:
            self.ongoing_warnings = {}


class BehaviourAnalyzer:
    """
    Wrapper around OpenCV Haar-cascade face detection with simple heuristics:
    - No face for N seconds
    - Multiple faces for N seconds
    - Face far from screen center for N seconds (looking away / head turned)
    """

    def __init__(
        self,
        no_face_threshold_s: float = 1.5,
        multiple_faces_threshold_s: float = 0.8,
        look_away_threshold_s: float = 1.2,
        eye_missing_threshold_s: float = 1.0,
        eye_away_threshold_s: float = 0.9,
        center_tolerance: float = 0.25,
        warning_emit_every_s: float = 1.0,
        detection_scale: float = 0.75,
    ) -> None:
        self.state = FaceEventState()
        self.no_face_delta = timedelta(seconds=no_face_threshold_s)
        self.multi_face_delta = timedelta(seconds=multiple_faces_threshold_s)
        self.look_away_delta = timedelta(seconds=look_away_threshold_s)
        self.eye_missing_delta = timedelta(seconds=eye_missing_threshold_s)
        self.eye_away_delta = timedelta(seconds=eye_away_threshold_s)
        self.center_tolerance = center_tolerance
        self.warning_emit_delta = timedelta(seconds=warning_emit_every_s)
        self.last_warning_emit: Dict[str, datetime] = {}
        self.detection_scale = detection_scale
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.center_history = deque(maxlen=5)

    def _set_warning(self, key: str, message: Optional[str]) -> None:
        if message:
            self.state.ongoing_warnings[key] = message
        else:
            self.state.ongoing_warnings.pop(key, None)

    def _emit_periodic_warning(
        self,
        now: datetime,
        event_type: str,
        details: str,
        student_name: Optional[str],
        severity: str = "warning",
    ) -> None:
        last = self.last_warning_emit.get(event_type)
        if last is None or now - last >= self.warning_emit_delta:
            log_event(
                "video",
                event_type,
                details,
                student_name=student_name,
                severity=severity,
            )
            self.last_warning_emit[event_type] = now

    def _detect_events(
        self,
        now: datetime,
        frame_size: Tuple[int, int],
        faces: List[Tuple[int, int, int, int]],
        gray_frame,
        student_name: Optional[str],
    ) -> None:
        width, height = frame_size
        face_count = len(faces)
        primary_face: Optional[Tuple[int, int, int, int]] = None
        if face_count >= 1:
            primary_face = max(faces, key=lambda f: f[2] * f[3])

        if face_count == 0:
            if self.state.last_no_face_time is None:
                self.state.last_no_face_time = now
            else:
                absent_for_s = (now - self.state.last_no_face_time).total_seconds()
                self._set_warning("face_absent", f"Warning: face missing ({absent_for_s:.1f}s)")
                if now - self.state.last_no_face_time >= self.no_face_delta:
                    self._emit_periodic_warning(
                        now,
                        "face_absent",
                        f"No face detected for {absent_for_s:.1f}s",
                        student_name=student_name,
                    )
        else:
            self.state.last_no_face_time = None
            self._set_warning("face_absent", None)

        if face_count > 1:
            if self.state.last_multiple_faces_time is None:
                self.state.last_multiple_faces_time = now
            else:
                multi_for_s = (now - self.state.last_multiple_faces_time).total_seconds()
                self._set_warning(
                    "multiple_faces",
                    f"Warning: multiple faces ({face_count}) for {multi_for_s:.1f}s",
                )
                if now - self.state.last_multiple_faces_time >= self.multi_face_delta:
                    self._emit_periodic_warning(
                        now,
                        "multiple_faces",
                        f"{face_count} faces detected for {multi_for_s:.1f}s",
                        student_name=student_name,
                        severity="critical",
                    )
        else:
            self.state.last_multiple_faces_time = None
            self._set_warning("multiple_faces", None)

        if primary_face is not None:
            x, y, w_box, h_box = primary_face
            cx = (x + w_box / 2.0) / float(width)
            cy = (y + h_box / 2.0) / float(height)
            self.center_history.append((cx, cy))
            avg_cx = sum(p[0] for p in self.center_history) / len(self.center_history)
            avg_cy = sum(p[1] for p in self.center_history) / len(self.center_history)

            if (
                avg_cx < 0.5 - self.center_tolerance
                or avg_cx > 0.5 + self.center_tolerance
                or avg_cy < 0.5 - self.center_tolerance
                or avg_cy > 0.5 + self.center_tolerance
            ):
                if self.state.last_look_away_time is None:
                    self.state.last_look_away_time = now
                else:
                    away_for_s = (now - self.state.last_look_away_time).total_seconds()
                    self._set_warning(
                        "looking_away",
                        f"Warning: looking away ({away_for_s:.1f}s)",
                    )
                    if now - self.state.last_look_away_time >= self.look_away_delta:
                        self._emit_periodic_warning(
                            now,
                            "head_movement",
                            f"Face away from center for {away_for_s:.1f}s",
                            student_name=student_name,
                        )
            else:
                self.state.last_look_away_time = None
                self._set_warning("looking_away", None)

            face_roi_gray = gray_frame[y : y + h_box, x : x + w_box]
            eyes = ()
            if face_roi_gray.size > 0:
                eyes = eye_cascade.detectMultiScale(
                    face_roi_gray, scaleFactor=1.1, minNeighbors=4, minSize=(16, 16)
                )
                if len(eyes) < 1:
                    eyes = eye_glasses_cascade.detectMultiScale(
                        face_roi_gray,
                        scaleFactor=1.1,
                        minNeighbors=4,
                        minSize=(16, 16),
                    )
            if len(eyes) < 1:
                self.state.last_eye_away_time = None
                self._set_warning("eyes_away", None)
                if self.state.last_eye_missing_time is None:
                    self.state.last_eye_missing_time = now
                else:
                    eye_missing_for_s = (
                        now - self.state.last_eye_missing_time
                    ).total_seconds()
                    self._set_warning(
                        "eyes_missing",
                        f"Warning: eyes not clearly visible ({eye_missing_for_s:.1f}s)",
                    )
                    if now - self.state.last_eye_missing_time >= self.eye_missing_delta:
                        self._emit_periodic_warning(
                            now,
                            "eyes_not_visible",
                            f"Eyes not visible for {eye_missing_for_s:.1f}s",
                            student_name=student_name,
                        )
            else:
                self.state.last_eye_missing_time = None
                self._set_warning("eyes_missing", None)

                # Eye-direction heuristic: if detected eye centers shift too far
                # from face center for a sustained period, flag as eyes looking away.
                eyes_sorted = sorted(eyes, key=lambda e: e[2] * e[3], reverse=True)[:2]
                eye_centers = [
                    (
                        (ex + ew / 2.0) / float(w_box),
                        (ey + eh / 2.0) / float(h_box),
                    )
                    for (ex, ey, ew, eh) in eyes_sorted
                ]
                avg_eye_x = sum(c[0] for c in eye_centers) / len(eye_centers)
                avg_eye_y = sum(c[1] for c in eye_centers) / len(eye_centers)
                eye_x_tolerance = 0.18
                eye_y_tolerance = 0.18

                if (
                    avg_eye_x < 0.5 - eye_x_tolerance
                    or avg_eye_x > 0.5 + eye_x_tolerance
                    or avg_eye_y < 0.35 - eye_y_tolerance
                    or avg_eye_y > 0.35 + eye_y_tolerance
                ):
                    if self.state.last_eye_away_time is None:
                        self.state.last_eye_away_time = now
                    else:
                        eye_away_for_s = (now - self.state.last_eye_away_time).total_seconds()
                        self._set_warning(
                            "eyes_away",
                            f"Warning: eye movement outside screen ({eye_away_for_s:.1f}s)",
                        )
                        if now - self.state.last_eye_away_time >= self.eye_away_delta:
                            self._emit_periodic_warning(
                                now,
                                "eye_looking_away",
                                f"Eyes looking away from center for {eye_away_for_s:.1f}s",
                                student_name=student_name,
                            )
                else:
                    self.state.last_eye_away_time = None
                    self._set_warning("eyes_away", None)
        else:
            self.state.last_look_away_time = None
            self.state.last_eye_missing_time = None
            self.state.last_eye_away_time = None
            self.center_history.clear()
            self._set_warning("looking_away", None)
            self._set_warning("eyes_missing", None)
            self._set_warning("eyes_away", None)

    def get_live_warnings(self) -> List[Dict[str, str]]:
        key_to_event_type = {
            "face_absent": "face_absent",
            "multiple_faces": "multiple_faces",
            "looking_away": "looking_away",
            "eyes_missing": "eyes_not_visible",
            "eyes_away": "eye_looking_away",
        }
        warnings: List[Dict[str, str]] = []
        for key, message in self.state.ongoing_warnings.items():
            if not message:
                continue
            event_type = key_to_event_type.get(key, key)
            severity = "critical" if key == "multiple_faces" else "warning"
            warnings.append(
                {
                    "source": "video",
                    "event_type": event_type,
                    "details": message,
                    "severity": severity,
                    "created_at": "LIVE",
                }
            )
        return warnings

    def analyze_and_annotate(self, frame, student_name: Optional[str] = None):
        """
        Run face detection, update behaviour events and draw simple overlays.
        Returns the annotated frame.
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = self.clahe.apply(gray)
        h, w = frame.shape[:2]
        now = datetime.utcnow()

        detect_w = max(1, int(w * self.detection_scale))
        detect_h = max(1, int(h * self.detection_scale))
        gray_small = cv2.resize(gray, (detect_w, detect_h), interpolation=cv2.INTER_LINEAR)

        faces = face_cascade.detectMultiScale(
            gray_small, scaleFactor=1.08, minNeighbors=5, minSize=(45, 45)
        )
        inv_scale = 1.0 / self.detection_scale
        faces_list: List[Tuple[int, int, int, int]] = [
            (
                int(x * inv_scale),
                int(y * inv_scale),
                int(w_box * inv_scale),
                int(h_box * inv_scale),
            )
            for (x, y, w_box, h_box) in faces
        ]
        faces_list.sort(key=lambda f: f[2] * f[3], reverse=True)

        self._detect_events(now, (w, h), faces_list, gray, student_name)

        for (x, y, w_box, h_box) in faces_list:
            cv2.rectangle(frame, (x, y), (x + w_box, y + h_box), (0, 255, 0), 2)

        return frame
