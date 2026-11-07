from __future__ import annotations

from pathlib import Path
import re
import threading

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"


class Recognizer:
    def __init__(self):
        self.recognizer = None
        self.lock = threading.Lock()

    @property
    def available(self) -> bool:
        return (MODEL_DIR / "model.int8.onnx").exists() and (MODEL_DIR / "tokens.txt").exists()

    def load(self) -> None:
        if not self.available:
            raise RuntimeError("本地语音模型尚未就绪，请运行 tools/prepare_model.py。")
        with self.lock:
            if self.recognizer is None:
                import sherpa_onnx
                self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(MODEL_DIR / "model.int8.onnx"), tokens=str(MODEL_DIR / "tokens.txt"),
                    num_threads=4, language="auto", use_itn=True, provider="cpu", debug=False)

    def transcribe(self, samples: np.ndarray) -> str:
        self.load()
        with self.lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            self.recognizer.decode_stream(stream)
            return re.sub(r"<\|[^>]*\|>", "", stream.result.text).strip()
