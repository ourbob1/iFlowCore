"""
Model backends, loaded lazily so the API starts instantly and tests never
import torch.

  HFTextGenerator  Hugging Face causal LM with a chat template (default
                   Qwen/Qwen2.5-1.5B-Instruct, Apache-2.0). Shared by the
                   receipt pipeline (text → JSON) and tax Q&A.
  HFVisionReader   Hugging Face image-text-to-text model (optional `vlm`
                   receipt backend).
  TesseractOCR     Tesseract via pytesseract (system package in the image).

`ModelRegistry` tracks load state for /v1/status and warms models in a
background thread at startup when RS_AI_PRELOAD is on.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)


class TextGenerator(Protocol):
    name: str

    def generate(self, system: str, user: str, *, max_new_tokens: int) -> str: ...


class ImageReader(Protocol):
    name: str

    def read(self, image: Any, system: str, prompt: str, *, max_new_tokens: int) -> str: ...


class OCREngine(Protocol):
    name: str

    def image_to_text(self, image: Any) -> str: ...


def _torch_dtype(torch: Any) -> Any:
    choice = os.environ.get("RS_AI_DTYPE", "float32").strip().lower()
    return {"bfloat16": torch.bfloat16, "float16": torch.float16, "auto": "auto"}.get(choice, torch.float32)


def _configure_threads(torch: Any) -> None:
    threads = os.environ.get("RS_AI_THREADS", "").strip()
    if threads.isdigit() and int(threads) > 0:
        torch.set_num_threads(int(threads))


class HFTextGenerator:
    def __init__(self, model_id: str, revision: str | None = None) -> None:
        self.name = model_id
        self.revision = revision
        self._model: Any = None
        self._tokenizer: Any = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            _configure_threads(torch)
            log.info("Loading %s", self.name)
            self._tokenizer = AutoTokenizer.from_pretrained(self.name, revision=self.revision)
            model = AutoModelForCausalLM.from_pretrained(self.name, revision=self.revision,
                                                         torch_dtype=_torch_dtype(torch), low_cpu_mem_usage=True)
            model.eval()
            self._model = model

    def generate(self, system: str, user: str, *, max_new_tokens: int) -> str:
        self.load()
        import torch

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        inputs = self._tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                                     return_dict=True)
        inputs = {k: v.to(self._model.device) for k, v in inputs.items()}
        with torch.inference_mode():
            out = self._model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False,
                                       pad_token_id=self._tokenizer.eos_token_id)
        new_tokens = out[0][inputs["input_ids"].shape[-1]:]
        return self._tokenizer.decode(new_tokens, skip_special_tokens=True).strip()


class HFVisionReader:
    def __init__(self, model_id: str, revision: str | None = None) -> None:
        self.name = model_id
        self.revision = revision
        self._model: Any = None
        self._processor: Any = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor

            _configure_threads(torch)
            log.info("Loading %s", self.name)
            self._processor = AutoProcessor.from_pretrained(self.name, revision=self.revision)
            model = AutoModelForImageTextToText.from_pretrained(self.name, revision=self.revision,
                                                                torch_dtype=_torch_dtype(torch), low_cpu_mem_usage=True)
            model.eval()
            self._model = model

    def read(self, image: Any, system: str, prompt: str, *, max_new_tokens: int) -> str:
        self.load()
        import torch

        messages = [
            {"role": "system", "content": [{"type": "text", "text": system}]},
            {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]},
        ]
        text = self._processor.apply_chat_template(messages, add_generation_prompt=True)
        inputs = self._processor(text=[text], images=[image], return_tensors="pt").to(self._model.device)
        with torch.inference_mode():
            out = self._model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        new_tokens = out[:, inputs["input_ids"].shape[-1]:]
        return self._processor.batch_decode(new_tokens, skip_special_tokens=True)[0].strip()


class TesseractOCR:
    def __init__(self, languages: str = "eng") -> None:
        self.name = f"tesseract:{languages}"
        self.languages = languages

    def load(self) -> None:
        import pytesseract

        pytesseract.get_tesseract_version()          # raises if the binary is missing

    def image_to_text(self, image: Any) -> str:
        import pytesseract

        # psm 4: a single column of variable-size text, which is what receipts are.
        return pytesseract.image_to_string(image, lang=self.languages, config="--oem 1 --psm 4")


class ModelRegistry:
    """Named components with load status: off | not_loaded | loading | ready | error."""

    def __init__(self) -> None:
        self._items: dict[str, Any] = {}
        self._status: dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, key: str, component: Any | None) -> None:
        with self._lock:
            self._items[key] = component
            self._status[key] = "off" if component is None else (
                "not_loaded" if hasattr(component, "load") else "ready")

    def get(self, key: str) -> Any | None:
        return self._items.get(key)

    def status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._status)

    def ensure(self, key: str) -> Any:
        """Load (if needed) and return a component; raises if it is off or broken."""
        component = self._items.get(key)
        if component is None:
            raise LookupError(f"{key} is turned off on this server")
        if self._status.get(key) in ("not_loaded", "error") and hasattr(component, "load"):
            self._load(key, component)
        if self._status.get(key, "").startswith("error"):
            raise RuntimeError(f"{key} failed to load")
        return component

    def _load(self, key: str, component: Any) -> None:
        with self._lock:
            self._status[key] = "loading"
        try:
            component.load()
        except Exception as exc:
            log.exception("Loading %s failed", key)
            with self._lock:
                self._status[key] = f"error: {type(exc).__name__}"
            return
        with self._lock:
            self._status[key] = "ready"

    def warm(self, keys: list[str] | None = None, *, spawn: Callable[..., Any] | None = None) -> None:
        def run() -> None:
            for key in keys or list(self._items):
                component = self._items.get(key)
                if component is not None and hasattr(component, "load") and self._status.get(key) == "not_loaded":
                    self._load(key, component)

        (spawn or (lambda fn: threading.Thread(target=fn, name="model-warmup", daemon=True).start()))(run)
