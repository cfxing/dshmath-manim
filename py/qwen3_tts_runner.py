#!/usr/bin/env python3
"""Persistent subprocess worker for the locally installed qwen-tts package.

The model is loaded once and one JSON request is handled per input line.  This
keeps the expensive 1.7B model out of the per-segment startup path.

Protocol: one JSON object per line.
  * responses go to a private dup of stdout — fd 1 itself is pointed at stderr,
    so the very noisy progress output of transformers / qwen-tts (Python or C
    level) can never pollute the protocol;
  * every request line is ``{"text", "voice", "language", "output"}`` plus an
    optional ``max_new_tokens``;
  * right after the model is ready one ``{"ok": true, "ready": true}`` line is
    emitted so the parent can tell "still loading" from "generating".
"""

import json
import os
import sys
import time
from contextlib import redirect_stdout


def _protocol_stream():
    """Turn stdout into a private protocol channel.

    A dup of the real stdout is kept for JSON responses and fd 1 is pointed at
    stderr, so transformers / qwen-tts can print to stdout from Python *or* from
    C without ever polluting the protocol.
    """
    try:
        protocol_fd = os.dup(1)
        os.dup2(2, 1)
        return os.fdopen(protocol_fd, "w", buffering=1)
    except OSError:
        return sys.stdout


def _emit(stream, payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def _apply_offline_mode() -> None:
    """父进程确认权重已在本地缓存时才打开离线模式。

    HF_HUB_OFFLINE 必须在下游库导入 huggingface_hub 之前生效（它读的是导入期常量），
    所以这里在 import qwen_tts 之前调用。
    """
    if os.environ.get("QWEN3_TTS_OFFLINE", "").strip().lower() not in ("1", "true", "yes"):
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _disable_hub_dependent_tokenizer_patch() -> None:
    """跳过 transformers 的 Mistral regex 补丁。

    transformers 4.57 的 ``PreTrainedTokenizerBase._patch_mistral_regex`` 在
    vocab_size > 100k 的 fast tokenizer 上会**无条件**调用 ``model_info()`` 请求
    Hub API，即使 local_files_only=True：

      * 有外网：多一次无谓的联网；
      * 无外网：每次加载要等好几轮 10 秒连接超时（旧版插件正是卡在这里，
        整次渲染超时、旁白消失）；
      * 开了 HF_HUB_OFFLINE：直接抛 OfflineModeIsEnabled 导致加载失败。

    该补丁只修正 Mistral 系 tokenizer 的 pre_tokenizer 正则，Qwen3-TTS 的
    BPE tokenizer 与之无关，跳过是行为等价的。
    """
    try:
        from transformers.tokenization_utils_base import PreTrainedTokenizerBase

        PreTrainedTokenizerBase._patch_mistral_regex = classmethod(
            lambda cls, tokenizer, *args, **kwargs: tokenizer
        )
    except Exception as exc:  # noqa: BLE001 - 补丁失败也不能影响加载
        print(f"[tts] could not disable the mistral regex patch: {exc}",
              file=sys.stderr, flush=True)


def _load_model():
    """Load the model from the local cache first.

    The weights are expected to be pre-downloaded (the plugin ships a local TTS
    environment), so local_files_only is the default: on a machine without
    internet the hub round-trip costs several minutes of retries per render.
    """
    _disable_hub_dependent_tokenizer_patch()
    from qwen_tts import Qwen3TTSModel
    import torch

    model_name = os.environ.get("QWEN3_TTS_MODEL") or "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    kwargs = {
        "device_map": os.environ.get("QWEN3_TTS_DEVICE", "cuda:0"),
        "dtype": getattr(torch, os.environ.get("QWEN3_TTS_DTYPE", "bfloat16")),
        "attn_implementation": None,
    }
    try:
        return Qwen3TTSModel.from_pretrained(model_name, local_files_only=True, **kwargs)
    except Exception as exc:  # noqa: BLE001 - cache miss, fall back to the hub
        print(f"[tts] local-only load failed ({exc}); retrying through the hub",
              file=sys.stderr, flush=True)
    return Qwen3TTSModel.from_pretrained(model_name, **kwargs)


def main() -> int:
    _apply_offline_mode()
    protocol = _protocol_stream()
    started = time.monotonic()
    # stdout/stderr carry progress output only; the protocol lives on its own fd.
    with redirect_stdout(sys.stderr):
        import soundfile as sf

        try:
            model = _load_model()
        except Exception as exc:  # noqa: BLE001 - report any load failure to the parent
            _emit(protocol, {"ok": False, "ready": False,
                             "error": f"model load failed: {type(exc).__name__}: {exc}"})
            return 1
        _emit(protocol, {"ok": True, "ready": True,
                         "load_seconds": round(time.monotonic() - started, 2)})

        for line in sys.stdin:
            if not line.strip():
                continue
            request_started = time.monotonic()
            try:
                request = json.loads(line)
                extra = {}
                if request.get("max_new_tokens"):
                    extra["max_new_tokens"] = int(request["max_new_tokens"])
                with redirect_stdout(sys.stderr):
                    wavs, sample_rate = model.generate_custom_voice(
                        text=request["text"],
                        speaker=request["voice"],
                        language=request.get("language", "Chinese"),
                        **extra,
                    )
                    sf.write(request["output"], wavs[0], sample_rate)
                response = {
                    "ok": True,
                    "sample_rate": sample_rate,
                    "seconds": round(time.monotonic() - request_started, 2),
                }
            except Exception as exc:  # noqa: BLE001 - one bad segment must not kill the worker
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _emit(protocol, response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
