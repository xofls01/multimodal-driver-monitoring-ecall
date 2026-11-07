"""실시간 프로그램에 붙이는 연결부.

수신 스레드는 push()만 호출하고, 판정은 전용 스레드에서 1초마다 돈다.
그래서 판정이 수신이나 화면 갱신을 막지 않는다.

    assessor = LiveAssessor(model_path="models/hr_classifier.pt", log_path="bio_status.jsonl")
    assessor.start()
    assessor.push(t, ecg_raw, ppg_raw)   # 샘플을 받을 때마다. t는 라즈베리파이 시각(초)
    status = assessor.latest()           # 화면을 그릴 때. 아직 결과가 없으면 None
    assessor.stop()
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from .config import Config
from .monitor import BioSignalMonitor, BioStatus


class LiveAssessor:
    def __init__(self, model_path: Optional[str] = None, log_path: Optional[str] = None,
                 cfg: Optional[Config] = None, period_s: float = 1.0,
                 ppg_encoder_path: Optional[str] = None, ppg_head_path: Optional[str] = None):
        self._monitor = BioSignalMonitor(cfg, log_path=log_path, model_path=model_path,
                                         ppg_encoder_path=ppg_encoder_path, ppg_head_path=ppg_head_path)
        self._period_s = period_s
        self._samples: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_t: Optional[float] = None
        self._last_received: Optional[float] = None
        self._latest: Optional[BioStatus] = None
        self.error: Optional[str] = None

    def push(self, t: float, ecg_raw: float, ppg_raw: float) -> None:
        self._samples.put((t, ecg_raw, ppg_raw))

    def latest(self) -> Optional[BioStatus]:
        return self._latest

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bio-assessor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._monitor.close()

    def step(self) -> Optional[BioStatus]:
        """쌓인 샘플을 넣고 한 번 판정한다."""
        received = False
        while True:
            try:
                t, ecg_raw, ppg_raw = self._samples.get_nowait()
            except queue.Empty:
                break
            self._monitor.push_ecg(t, ecg_raw)
            self._monitor.push_ppg(t, ppg_raw)
            self._last_t = t
            received = True
        if self._last_t is None:
            return None
        if received:
            self._last_received = time.monotonic()
        # 샘플이 끊겨도 시간이 흐르게 해서 센서 끊김을 판정할 수 있게 한다
        now = self._last_t + (time.monotonic() - self._last_received)
        self._latest = self._monitor.update(now)
        return self._latest

    def _run(self) -> None:
        while not self._stop.wait(self._period_s):
            try:
                self.step()
                self.error = None
            except Exception as exc:  # 판정 오류로 측정과 녹화까지 멈추면 안 된다
                self.error = str(exc)
