from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import base64
import json
import subprocess
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RATE = 16000


@dataclass
class Utterance:
    source: str
    start: float
    end: float
    samples: np.ndarray


def mono_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim == 2:
        samples = samples.mean(axis=1)
    samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
    if rate == RATE:
        return samples
    if rate % RATE == 0:
        factor = rate // RATE
        n = len(samples) // factor * factor
        return samples[:n].reshape(-1, factor).mean(axis=1)
    size = round(len(samples) * RATE / rate)
    if not size:
        return np.empty(0, dtype=np.float32)
    return np.interp(np.arange(size) * rate / RATE, np.arange(len(samples)), samples).astype(np.float32)


class Segmenter:
    """Endpoint audio on silence; timestamps retain the session's wall-time axis."""

    def __init__(self, source: str, emit: Callable[[Utterance], None], max_seconds: float = 10.0):
        self.source = source
        self.emit = emit
        self.max_seconds = max_seconds
        self.pre: deque[tuple[np.ndarray, float]] = deque(maxlen=3)
        self.parts: list[np.ndarray] = []
        self.start = 0.0
        self.end = 0.0
        self.silence = 0.0
        self.count = 0
        self.lock = threading.RLock()

    def feed(self, samples: np.ndarray, start: float) -> None:
        with self.lock:
            for offset in range(0, len(samples), 1600):
                frame = samples[offset:offset + 1600]
                if not len(frame):
                    continue
                stamp = start + offset / RATE
                duration = len(frame) / RATE
                voiced = float(np.sqrt(np.mean(frame * frame))) >= 0.006
                if not self.parts:
                    if not voiced:
                        self.pre.append((frame.copy(), stamp))
                        continue
                    if self.pre:
                        self.start = self.pre[0][1]
                        self.parts.extend(part for part, _ in self.pre)
                        self.count = sum(len(part) for part in self.parts)
                        self.pre.clear()
                    else:
                        self.start = stamp
                self.parts.append(frame.copy())
                self.count += len(frame)
                self.end = stamp + duration
                self.silence = 0 if voiced else self.silence + duration
                if self.silence >= 0.65 or self.count / RATE >= self.max_seconds:
                    self.flush()

    def flush(self) -> None:
        with self.lock:
            if self.parts and self.count >= RATE * 0.3:
                item = Utterance(self.source, max(0, self.start), max(0, self.end), np.concatenate(self.parts))
                self.emit(item)
            self.parts = []
            self.pre.clear()
            self.count = 0
            self.silence = 0


def meeting_processes() -> list[dict]:
    # One Toolhelp snapshot avoids opening every process to query its parent.
    # On hosts with thousands of processes, per-process parent queries can take tens of seconds.
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("defaultHeap", ctypes.c_size_t), ("moduleID", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("ppid", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    candidates = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.name.lower() in {"wemeetapp.exe", "voovmeeting.exe"}:
                candidates.append({"pid": int(entry.pid), "ppid": int(entry.ppid), "name": entry.name})
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error not in {0, 18}:
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(snapshot)
    ids = {p["pid"] for p in candidates}
    return [{"pid": p["pid"], "name": p["name"], "parent_pid": p["ppid"], "scope": "process_and_children"}
            for p in candidates if p["ppid"] not in ids]


def microphones() -> list[dict]:
    import sounddevice as sd
    apis = sd.query_hostapis()
    devices = sd.query_devices()
    preferred = [{"id": i, "name": d["name"], "sample_rate": int(d["default_samplerate"]), "channels": d["max_input_channels"]}
                 for i, d in enumerate(devices) if d["max_input_channels"] > 0 and "WASAPI" in apis[d["hostapi"]]["name"]]
    if preferred:
        return preferred
    index = int(sd.default.device[0])
    if index < 0:
        return []
    d = devices[index]
    return [{"id": index, "name": d["name"], "sample_rate": int(d["default_samplerate"]), "channels": d["max_input_channels"]}]


class Capture:
    def __init__(self, pid: int, microphone: bool, microphone_id: int | None,
                 origin: float, submit: Callable[[Utterance], None], on_event: Callable[[dict], None]):
        self.pid = pid
        self.microphone = microphone
        self.microphone_id = microphone_id
        self.origin = origin
        self.submit = submit
        self.on_event = on_event
        self.remote = Segmenter("meeting_audio", submit)
        self.local = Segmenter("microphone", submit)
        self.process: subprocess.Popen | None = None
        self.stream = None
        self.reader: threading.Thread | None = None
        self.ready = threading.Event()
        self.failed: str | None = None
        self.closed = threading.Event()
        self.received_frames = 0
        self.levels = {"meeting_audio": 0.0, "microphone": 0.0}

    def start(self) -> None:
        helper = ROOT / "native/MeetingAudio/bin/Release/net9.0-windows/MeetingAudio.exe"
        if not helper.exists():
            raise RuntimeError("音频组件未编译，请运行 tools/bootstrap.ps1。")
        self.process = subprocess.Popen([str(helper), "--pid", str(self.pid)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        self.reader = threading.Thread(target=self._read, name="meeting-audio", daemon=True)
        self.reader.start()
        if not self.ready.wait(15):
            self.stop()
            raise RuntimeError("会议音频组件启动超时。")
        if self.failed:
            self.stop()
            raise RuntimeError(self.failed)
        try:
            if self.microphone:
                import sounddevice as sd
                choices = microphones()
                index = self.microphone_id if self.microphone_id is not None else (choices[0]["id"] if choices else None)
                if index is None:
                    raise RuntimeError("未找到麦克风。可关闭 include_microphone 后仅采集会议音频。")
                device = sd.query_devices(index, "input")
                rate = int(device["default_samplerate"])
                self.stream = sd.InputStream(device=index, samplerate=rate, channels=1, dtype="float32", blocksize=max(1, rate // 10),
                                             callback=lambda data, frames, timing, status: self._mic(data, rate, status))
                self.stream.start()
                self.on_event({"type": "microphone_ready", "device_id": index, "name": device["name"]})
        except Exception:
            self.stop()
            raise

    def _read(self) -> None:
        try:
            assert self.process and self.process.stdout
            for line in self.process.stdout:
                item = json.loads(line)
                if item["type"] == "ready":
                    self.ready.set()
                elif item["type"] == "error":
                    self.failed = item["message"]
                    self.ready.set()
                    self.on_event({"type": "capture_error", "message": self.failed})
                elif item["type"] == "audio" and not self.closed.is_set():
                    raw = np.frombuffer(base64.b64decode(item["pcm"]), dtype="<i2").reshape(-1, 2)
                    samples = mono_16k(raw.astype(np.float32) / 32768, 48000)
                    self.received_frames += len(raw)
                    self.levels["meeting_audio"] = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0
                    # Windows supplies QPC timestamps in 100ns units; use receive time when flagged invalid.
                    stamp = item.get("qpc_100ns", 0) / 1e7
                    if not stamp or item.get("flags", 0) & 4 or abs(stamp - time.perf_counter()) > 5:
                        stamp = time.perf_counter() - len(samples) / RATE
                    self.remote.feed(samples, stamp - self.origin)
                    if item.get("flags", 0) & 1:
                        self.on_event({"type": "audio_discontinuity", "source": "meeting_audio", "at": max(0, stamp-self.origin)})
            if not self.closed.is_set() and not self.failed:
                self.failed = "会议音频组件意外退出，采集已停止。"
                self.on_event({"type": "capture_error", "message": self.failed})
        except Exception as exc:
            self.failed = str(exc)
            self.on_event({"type": "capture_error", "message": self.failed})
        finally:
            self.ready.set()

    def _mic(self, data: np.ndarray, rate: int, status) -> None:
        if self.closed.is_set():
            return
        samples = mono_16k(data.copy(), rate)
        self.levels["microphone"] = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0
        if status:
            self.on_event({"type": "audio_discontinuity", "source": "microphone", "detail": str(status)})
        self.local.feed(samples, time.perf_counter() - self.origin - len(samples) / RATE)

    def stop(self) -> None:
        self.closed.set()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if self.process is not None:
            try:
                if self.process.stdin and self.process.poll() is None:
                    self.process.stdin.write(b"stop\n")
                    self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=3)
            if self.reader and self.reader is not threading.current_thread():
                self.reader.join(timeout=3)
            for pipe in [self.process.stdin, self.process.stdout, self.process.stderr]:
                if pipe:
                    pipe.close()
            self.process = None
        self.remote.flush()
        self.local.flush()
