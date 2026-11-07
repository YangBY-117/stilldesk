from __future__ import annotations

from pathlib import Path
import hashlib
import queue
import threading
import time
import wave

import numpy as np

from .asr import Recognizer, ROOT
from .audio import Capture, RATE, Segmenter, Utterance, meeting_processes, microphones, mono_16k
from .store import Session, TERMINAL, now, safe_session_id
from .state import sessions_directory, private_directory


class Engine:
    def __init__(self, root: Path | None = None, recognizer=None):
        self.root = private_directory(root) if root is not None else sessions_directory()
        self.recognizer = recognizer or Recognizer()
        self.sessions: dict[str, Session] = {}
        self.jobs: queue.Queue[tuple[Session, Utterance] | None] = queue.Queue(maxsize=64)
        self.pending: dict[str, int] = {}
        self.active: str | None = None
        self.capture: Capture | None = None
        self.origin = 0.0
        self.lock = threading.RLock()
        self.control = threading.RLock()
        self.closed = False
        for folder in sorted(self.root.iterdir()):
            if folder.is_dir() and (folder / "session.json").exists():
                session = Session(folder)
                if session.meta["state"] not in TERMINAL:
                    session.event({"type": "interrupted", "message": "服务重新启动；未自动恢复录音，已保留此前转写。"})
                    session.update(state="interrupted", ended_at=now())
                self.sessions[session.meta["session_id"]] = session
        self.worker = threading.Thread(target=self._worker, name="local-asr", daemon=True)
        self.worker.start()

    def session(self, sid: str) -> Session:
        safe_session_id(sid)
        try:
            return self.sessions[sid]
        except KeyError as exc:
            raise ValueError("没有找到这场会议。") from exc

    def devices(self) -> dict:
        return {"meeting_processes": meeting_processes(), "microphones": microphones(),
                "capture_scope": "selected_process_and_children", "asr_local": True, "model_ready": self.recognizer.available,
                "microphone_note": "本机麦克风是独立声源；使用耳机可降低远端声音串入麦克风。"}

    def list_sessions(self) -> dict:
        return {"sessions": [self.status(sid) for sid in reversed(list(self.sessions))], "active_session_id": self.active}

    def status(self, sid: str) -> dict:
        session = self.session(sid)
        with self.lock:
            result = {**session.snapshot(), "pending_utterances": self.pending.get(sid, 0)}
            if sid == self.active and self.capture:
                result.update(audio_levels=dict(self.capture.levels), remote_audio_frames=self.capture.received_frames)
            return result

    def _new(self, title: str, mode: str, settings: dict) -> Session:
        with self.lock:
            if self.active is not None:
                raise ValueError("已有会议正在采集或整理，请先结束并等待处理完成。")
            session = Session.create(self.root, title, mode, settings)
            sid = session.meta["session_id"]
            self.sessions[sid] = session
            self.pending[sid] = 0
            self.active = sid
            self.origin = time.perf_counter()
            return session

    def start(self, title: str, include_microphone: bool = False, microphone_id: int | None = None,
              target_pid: int | None = None, save_audio: bool = False) -> dict:
        with self.control:
            candidates = meeting_processes()
            ids = {p["pid"] for p in candidates}
            if target_pid is None:
                if len(candidates) != 1:
                    raise ValueError("未找到唯一的腾讯会议进程，请调用 devices 并指定 target_pid。")
                target_pid = candidates[0]["pid"]
            if target_pid not in ids:
                raise ValueError("target_pid 必须是 devices 返回的腾讯会议根进程。")
            self.recognizer.load()
            session = self._new(title, "live", {"include_microphone": include_microphone, "microphone_id": microphone_id,
                                "target_pid": target_pid, "save_audio": save_audio, "scope": "process_and_children"})
            try:
                self._capture(session)
                session.update(state="recording")
                session.event({"type": "started", "message": "仅采集选定腾讯会议进程树；未区分远端发言人。"})
            except Exception as exc:
                session.event({"type": "capture_error", "message": str(exc)})
                session.update(state="failed", ended_at=now())
                with self.lock:
                    self.active = None
                raise
            return self.status(session.meta["session_id"])

    def _capture(self, session: Session) -> None:
        settings = session.meta["settings"]
        capture = Capture(settings["target_pid"], settings["include_microphone"], settings["microphone_id"], self.origin,
                          lambda item: self.submit(session, item), lambda event: self._capture_event(session, event))
        self.capture = capture
        capture.start()

    def _capture_event(self, session: Session, event: dict) -> None:
        session.event(event)
        if event["type"] == "capture_error":
            threading.Thread(target=self._fail_capture, args=(session,), daemon=True).start()

    def _fail_capture(self, session: Session) -> None:
        with self.control:
            if self.active != session.meta["session_id"] or session.meta["state"] not in {"recording", "starting"}:
                return
            if self.capture:
                self.capture.stop()
                self.capture = None
            session.update(state="draining", terminal_state="failed")
            self._finish_if_ready(session)

    def pause(self, sid: str) -> dict:
        with self.control:
            session = self.session(sid)
            if sid != self.active or session.meta["state"] != "recording":
                raise ValueError("仅正在采集的会议可以暂停。")
            if self.capture:
                self.capture.stop()
                self.capture = None
            session.update(state="paused")
            session.event({"type": "paused", "at_seconds": round(time.perf_counter()-self.origin, 3), "message": "已释放会议音频与麦克风采集；暂停期间没有录音。"})
            return self.status(sid)

    def resume(self, sid: str) -> dict:
        with self.control:
            session = self.session(sid)
            if sid != self.active or session.meta["state"] != "paused":
                raise ValueError("仅暂停状态可以继续。")
            self._capture(session)
            session.update(state="recording")
            session.event({"type": "resumed", "at_seconds": round(time.perf_counter()-self.origin, 3)})
            return self.status(sid)

    def stop(self, sid: str) -> dict:
        with self.control:
            session = self.session(sid)
            if session.meta["state"] in TERMINAL | {"draining"}:
                return self.status(sid)
            if sid != self.active:
                raise ValueError("会议不在当前采集服务中。")
            if session.meta["mode"] == "import" and session.meta["state"] == "importing":
                raise ValueError("正在处理导入文件，请等待完成。")
            if self.capture:
                self.capture.stop()
                self.capture = None
            session.event({"type": "stopped", "at_seconds": round(time.perf_counter()-self.origin, 3)})
            session.update(state="draining")
            self._finish_if_ready(session)
            return self.status(sid)

    def submit(self, session: Session, item: Utterance) -> None:
        sid = session.meta["session_id"]
        with self.lock:
            self.pending[sid] = self.pending.get(sid, 0) + 1
            try:
                self.jobs.put_nowait((session, item))
            except queue.Full:
                self.pending[sid] -= 1
                session.event({"type": "queue_overflow", "source": item.source, "start_seconds": item.start, "end_seconds": item.end,
                               "message": "识别积压达到上限；该段未识别，已停止继续采集，不能把本场稿件视为完整。"})
                threading.Thread(target=self._fail_capture, args=(session,), daemon=True).start()

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                return
            session, item = job
            sid = session.meta["session_id"]
            try:
                began = time.perf_counter()
                audio_path = None
                if session.meta["settings"].get("save_audio"):
                    directory = session.directory / "audio"
                    private_directory(directory)
                    audio_path = directory / f"{time.time_ns()}-{item.source}.wav"
                    with wave.open(str(audio_path), "wb") as output:
                        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                        output.writeframes((np.clip(item.samples, -1, 1)*32767).astype("<i2").tobytes())
                text = self.recognizer.transcribe(item.samples)
                if text:
                    session.append(item.source, item.start, item.end, text,
                                   recognition_seconds=round(time.perf_counter()-began, 3),
                                   audio_path=str(audio_path) if audio_path else None)
            except Exception as exc:
                session.event({"type": "asr_error", "source": item.source, "start_seconds": item.start, "end_seconds": item.end, "message": str(exc)})
            finally:
                with self.lock:
                    self.pending[sid] -= 1
                self.jobs.task_done()
                self._finish_if_ready(session)

    def _finish_if_ready(self, session: Session) -> None:
        with self.lock:
            sid = session.meta["session_id"]
            if session.meta["state"] == "draining" and self.pending.get(sid, 0) == 0:
                issues = any(e["type"] in {"asr_error", "queue_overflow", "capture_error"} for e in session.events)
                session.update(state=session.meta.get("terminal_state", "finished"), ended_at=now(), coverage="has_errors" if issues else "captured_intervals_only")
                session.export()
                if self.active == sid:
                    self.active = None

    def import_audio(self, path: str, title: str, save_audio: bool = False) -> dict:
        with self.control:
            file = Path(path).expanduser().resolve(strict=True)
            if not file.is_file() or file.suffix.lower() not in {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg"}:
                raise ValueError("请提供本地 WAV、MP3、M4A、MP4、FLAC 或 OGG 文件。")
            if file.stat().st_size > 1024**3:
                raise ValueError("第一版单文件上限为 1 GiB，请先分段。")
            self.recognizer.load()
            session = self._new(title, "import", {"input_path": str(file), "input_bytes": file.stat().st_size, "save_audio": save_audio})
            session.update(state="importing")
            threading.Thread(target=self._import, args=(session, file), name="audio-import", daemon=True).start()
            return self.status(session.meta["session_id"])

    def _import(self, session: Session, file: Path) -> None:
        try:
            import av
            digest = hashlib.sha256()
            with file.open("rb") as source:
                for chunk in iter(lambda: source.read(1024*1024), b""):
                    digest.update(chunk)
            session.update(input_sha256=digest.hexdigest())
            # Streaming decode bounds memory independently of meeting length.
            with av.open(str(file)) as container:
                if not container.streams.audio:
                    raise ValueError("文件中没有音轨。")
                resampler = av.AudioResampler(format="fltp", layout="mono", rate=RATE)
                segmenter = Segmenter("imported_audio", lambda item: self._submit_import(session, item))
                offset = 0
                for frame in container.decode(audio=0):
                    for converted in resampler.resample(frame):
                        audio = converted.to_ndarray().reshape(-1).astype(np.float32)
                        segmenter.feed(audio, offset / RATE)
                        offset += len(audio)
                for converted in resampler.resample(None):
                    audio = converted.to_ndarray().reshape(-1).astype(np.float32)
                    segmenter.feed(audio, offset / RATE)
                    offset += len(audio)
                segmenter.flush()
            session.update(state="draining", duration_seconds=round(offset/RATE, 3))
        except Exception as exc:
            session.event({"type": "asr_error", "message": str(exc)})
            session.update(state="draining", terminal_state="failed")
        self._finish_if_ready(session)

    def _submit_import(self, session: Session, item: Utterance) -> None:
        sid = session.meta["session_id"]
        with self.lock:
            self.pending[sid] = self.pending.get(sid, 0) + 1
        self.jobs.put((session, item))

    def close(self) -> None:
        with self.control:
            if self.active:
                session = self.session(self.active)
                if session.meta["mode"] == "live":
                    self.stop(self.active)
                elif session.meta["state"] not in TERMINAL:
                    raise ValueError("导入任务仍在处理中，不能关闭服务。")
            self.jobs.join()
            self.jobs.put(None)
            self.worker.join(timeout=5)
            self.closed = True
