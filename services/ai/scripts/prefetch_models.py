"""Download the configured models into the HF cache volume ahead of time.

    docker compose run --rm ai python scripts/prefetch_models.py
"""

from __future__ import annotations

import os

from huggingface_hub import snapshot_download


def main() -> None:
    wanted = []
    if os.environ.get("RS_AI_RECEIPT_BACKEND", "ocr_llm") == "ocr_llm" or os.environ.get("RS_AI_TAX_BACKEND", "llm") == "llm":
        wanted.append((os.environ.get("RS_AI_LLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"),
                       os.environ.get("RS_AI_LLM_REVISION") or None))
    if os.environ.get("RS_AI_RECEIPT_BACKEND") == "vlm":
        wanted.append((os.environ.get("RS_AI_VLM_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct"),
                       os.environ.get("RS_AI_VLM_REVISION") or None))
    for repo, revision in wanted:
        path = snapshot_download(repo, revision=revision,
                                 allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.tiktoken", "*.jinja"])
        print(f"{repo}@{revision or 'main'} → {path}")


if __name__ == "__main__":
    main()
