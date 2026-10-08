#!/usr/bin/env python3
"""Persistent subprocess worker for the locally installed qwen-tts package.

The model is loaded once and one JSON request is handled per input line.  This
keeps the expensive 1.7B model out of the per-segment startup path.
"""

import json
import os
import sys


def main() -> int:
    from qwen_tts import Qwen3TTSModel
    import soundfile as sf

    import torch

    model_name = os.environ.get("QWEN3_TTS_MODEL") or "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    model = Qwen3TTSModel.from_pretrained(
        model_name,
        device_map=os.environ.get("QWEN3_TTS_DEVICE", "cuda:0"),
        dtype=getattr(torch, os.environ.get("QWEN3_TTS_DTYPE", "bfloat16")),
        attn_implementation=None,
    )
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            wavs, sample_rate = model.generate_custom_voice(
                text=request["text"],
                speaker=request["voice"],
                language=request.get("language", "Chinese"),
            )
            sf.write(request["output"], wavs[0], sample_rate)
            response = {"ok": True, "sample_rate": sample_rate}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
