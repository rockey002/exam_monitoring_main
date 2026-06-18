import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd

from monitoring_db import log_event


class AudioMonitor:
    """
    Simple background audio monitor that flags loud environments as suspicious.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        block_duration_s: float = 0.5,
        rms_threshold: float = 0.05,
        speech_rms_min: float = 0.015,
        speech_rms_max: float = 0.08,
        speech_duration_s: float = 3.0,
        talk_rms_min: float = 0.02,
        talk_duration_s: float = 2.5,
        cooldown_s: float = 5.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_duration_s = block_duration_s
        self.block_size = int(sample_rate * block_duration_s)
        self.rms_threshold = rms_threshold
        self.speech_rms_min = speech_rms_min
        self.speech_rms_max = speech_rms_max
        self.speech_duration_s = speech_duration_s
        self.talk_rms_min = talk_rms_min
        self.talk_duration_s = talk_duration_s
        self.cooldown_s = cooldown_s
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_event_time: float = 0.0
        self._speech_active_since: Optional[float] = None
        self._talk_active_since: Optional[float] = None
        self._last_voice_event_time: float = 0.0
        self._last_talk_event_time: float = 0.0
        self._active_student_name: Optional[str] = None
        self._active_student_lock = threading.Lock()

    def set_active_student(self, student_name: Optional[str]) -> None:
        clean_name = (student_name or "").strip().lower() or None
        with self._active_student_lock:
            self._active_student_name = clean_name

    def _get_active_student(self) -> Optional[str]:
        with self._active_student_lock:
            return self._active_student_name

    @staticmethod
    def _rms_to_db(rms: float) -> float:
        return 20.0 * float(np.log10(max(rms, 1e-8)))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        try:
            with sd.InputStream(
                channels=1,
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                dtype="float32",
            ):
                while self._running:
                    data, _ = sd.rec(
                        frames=self.block_size,
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype="float32",
                    ), None
                    sd.wait()
                    rms = float(np.sqrt(np.mean(np.square(data))))
                    db_level = self._rms_to_db(rms)
                    student_name = self._get_active_student()
                    now = time.time()
                    if rms > self.rms_threshold and now - self._last_event_time > self.cooldown_s:
                        self._last_event_time = now
                        log_event(
                            "audio",
                            "loud_noise",
                            f"Loud background noise detected (RMS={rms:.3f}, dBFS={db_level:.1f})",
                            student_name=student_name,
                            severity="critical",
                        )

                    speech_like = self.speech_rms_min <= rms <= self.speech_rms_max
                    if speech_like:
                        if self._speech_active_since is None:
                            self._speech_active_since = now
                        speech_for_s = now - self._speech_active_since
                        if (
                            speech_for_s >= self.speech_duration_s
                            and now - self._last_voice_event_time > self.cooldown_s
                        ):
                            self._last_voice_event_time = now
                            log_event(
                                "audio",
                                "voice_activity",
                                f"Possible speaking detected for {speech_for_s:.1f}s (RMS={rms:.3f}, dBFS={db_level:.1f})",
                                student_name=student_name,
                                severity="warning",
                            )
                    else:
                        self._speech_active_since = None

                    # Explicit unwanted talk warning if student voice persists.
                    if rms >= self.talk_rms_min:
                        if self._talk_active_since is None:
                            self._talk_active_since = now
                        talk_for_s = now - self._talk_active_since
                        if (
                            talk_for_s >= self.talk_duration_s
                            and now - self._last_talk_event_time > self.cooldown_s
                        ):
                            self._last_talk_event_time = now
                            log_event(
                                "audio",
                                "unwanted_talk",
                                f"Unwanted talking detected for {talk_for_s:.1f}s (RMS={rms:.3f}, dBFS={db_level:.1f})",
                                student_name=student_name,
                                severity="critical",
                            )
                    else:
                        self._talk_active_since = None
                    time.sleep(0.1)
        except Exception:
            # If audio capture fails (no microphone, permissions, etc.), we silently disable audio monitoring.
            self._running = False
