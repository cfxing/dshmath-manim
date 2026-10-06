#!/usr/bin/env python3
"""Small subprocess adapter for the locally installed qwen-tts package."""

import json
import os
import sys


def main() -> int:
    request = json.load(sys.stdin)
    from qwen_tts import Qwen3TTSModel
    import soundfile as sf

    import torch

    model = Qwen3TTSModel.from_pretrained(
        request["model"],
        device_map=os.environ.get("QWEN3_TTS_DEVICE", "cuda:0"),
        dtype=getattr(torch, os.environ.get("QWEN3_TTS_DTYPE", "bfloat16")),
        attn_implementation=None,
    )
    wavs, sample_rate = model.generate_custom_voice(
        text=request["text"],
        speaker=request.get("voice", "Vivian"),
        language=request.get("language", "Chinese"),
    )
    sf.write(request["output"], wavs[0], sample_rate)
    print(json.dumps({"ok": True, "sample_rate": sample_rate}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
