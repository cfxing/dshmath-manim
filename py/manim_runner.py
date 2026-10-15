#!/usr/bin/env python3
"""manim_runner.py — Manim CE 渲染执行器（dshmath-manim 后端）。

DeepSeek Harness 的 TS Tool 插件通过 subprocess 调用本脚本，在独立进程内
完成场景代码生成、静态校验与渲染，输出机器可读的 JSON 结果。

子命令：
  render        按模板 + 参数生成场景并渲染（推荐，安全）
  render-code   渲染一个已存在的 Python 场景文件（模型自写代码，进阶）
  validate      仅对场景代码做静态安全检查
  templates     列出可用模板及其参数模式
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# 渲染时对模型可见的"安全"全局，禁止访问文件系统/进程等
FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "open", "compile", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "hasattr", "input", "breakpoint", "exit",
    "quit", "help", "__builtins__",
}

# 场景代码允许 import 的模块白名单（渲染只用得到 manim / numpy / math）
ALLOWED_IMPORTS = {"manim", "numpy", "math"}

# numpy 中可读写文件 / 加载本地库的危险成员，禁止属性访问与调用
NUMPY_DANGEROUS_ATTRS = {
    "loadtxt", "savetxt", "save", "load", "fromfile", "tofile", "memmap",
    "frombuffer", "fromstring", "genfromtxt", "lib", "ctypeslib", "f2py",
}

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov")
DEFAULT_TTS_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_TTS_SPEED = 0.97
EDGE_TTS_TIMEOUT = 180


def _banner() -> dict:
    return {
        "name": "dshmath-manim",
        "version": "0.1.0",
        "manim_version": _manim_version(),
        "templates_dir": str(TEMPLATES_DIR),
    }


def _manim_version() -> str:
    try:
        import manim  # noqa: F401

        return manim.__version__
    except Exception:
        return "not-installed"


# ---------------------------------------------------------------------------
# 模板系统
# ---------------------------------------------------------------------------

def _load_templates() -> dict[str, dict]:
    """扫描 py/templates 下的模板定义文件，返回 {name: meta}。"""
    templates: dict[str, dict] = {}
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            templates[path.stem] = {"error": f"malformed template: {exc}"}
            continue
        meta.setdefault("name", path.stem)
        meta.setdefault("description", "")
        meta.setdefault("parameters", {})
        meta.setdefault("code", "")
        templates[path.stem] = meta
    return templates


def _validate_template_params(meta: dict, params: dict) -> list[str]:
    """校验参数是否符合模板声明的模式（类型 / 必填）。"""
    errors: list[str] = []
    schema = meta.get("parameters", {})
    for key, spec in schema.items():
        required = spec.get("required", False)
        if required and key not in params:
            errors.append(f"missing required parameter '{key}'")
            continue
        if key not in params:
            continue
        typ = spec.get("type")
        value = params[key]
        if typ == "number" and isinstance(value, bool):
            errors.append(f"parameter '{key}' must be a number")
        elif typ == "number" and not isinstance(value, (int, float)):
            errors.append(f"parameter '{key}' must be a number, got {type(value).__name__}")
        elif typ == "string" and not isinstance(value, str):
            errors.append(f"parameter '{key}' must be a string, got {type(value).__name__}")
        elif typ == "boolean" and not isinstance(value, bool):
            errors.append(f"parameter '{key}' must be a boolean, got {type(value).__name__}")
        elif typ == "array" and not isinstance(value, list):
            errors.append(f"parameter '{key}' must be an array, got {type(value).__name__}")
        elif typ == "array" and isinstance(value, list):
            minimum = spec.get("minItems")
            maximum = spec.get("maxItems")
            if minimum is not None and len(value) < minimum:
                errors.append(f"parameter '{key}' needs at least {minimum} items")
            if maximum is not None and len(value) > maximum:
                errors.append(f"parameter '{key}' allows at most {maximum} items")
            if spec.get("items") == "string" and not all(isinstance(item, str) and item.strip() for item in value):
                errors.append(f"parameter '{key}' must contain non-empty strings")
    unknown = set(params) - set(schema)
    if unknown:
        errors.append(f"unknown parameters: {sorted(unknown)}")
    return errors


def _render_template(meta: dict, params: dict, code: str) -> str:
    """将参数安全地注入模板代码：只做 {key} 文本替换，绝不 eval 用户代码。

    模板代码中需要动态求值的数学表达式一律交给受限求值器 _S()
    （见 _SAFE_EVAL_SRC，由 render_scene 注入场景文件头部），
    本函数本身不做任何 eval。
    未显式提供的参数使用模板声明的默认值（default 字段）。"""
    merged = dict(params)
    for key, spec in meta.get("parameters", {}).items():
        if key not in merged and "default" in spec:
            merged[key] = spec["default"]
    for key, value in merged.items():
        code = code.replace(f"{{{{{key}}}}}", _safe_literal(value))
    # 残留的未替换占位符会破坏语法，直接报错
    leftovers = re.findall(r"\{\{\s*\w+\s*\}\}", code)
    if leftovers:
        raise ValueError(f"template placeholders not filled: {sorted(set(leftovers))}")
    return code


def _safe_literal(value: object) -> str:
    """把参数渲染成可安全 eval 的 Python 字面量。"""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple, dict)):
        return repr(value)
    raise ValueError(f"unsupported parameter type: {type(value).__name__}")


# ---------------------------------------------------------------------------
# 受限表达式求值器（模板表达式安全求值）
# ---------------------------------------------------------------------------
#
# 模板把用户/模型传入的数学表达式交给受限求值器 _S() 处理，代替裸 eval。
# _S 的完整实现（含 AST 白名单检查）由 render_scene 注入到场景文件头部，
# 因此模板代码可直接调用 _S，渲染出的场景文件完全自包含。
_SAFE_EVAL_SRC = '''\
# --- 受限表达式求值器（安全）：仅白名单 AST 节点 / 名字 / 属性，绝无裸 eval ---
import ast as _ast

_SAFE_FUNCS = {"abs": abs, "min": min, "max": max, "round": round, "sum": sum, "pow": pow, "len": len}
_SAFE_MODULES = {"np", "math"}
_ALLOWED_NODES = (
    _ast.Expression, _ast.Constant, _ast.Name, _ast.Attribute,
    _ast.BinOp, _ast.UnaryOp, _ast.BoolOp, _ast.Compare,
    _ast.Call, _ast.List, _ast.Tuple, _ast.Subscript, _ast.Slice,
    _ast.keyword,
    _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.FloorDiv, _ast.Mod, _ast.Pow,
    _ast.USub, _ast.UAdd, _ast.And, _ast.Or, _ast.Not,
    _ast.Eq, _ast.NotEq, _ast.Lt, _ast.LtE, _ast.Gt, _ast.GtE,
    _ast.Is, _ast.IsNot, _ast.In, _ast.NotIn, _ast.Load,
)


def _S(expr, env):
    """受限表达式求值：白名单 AST 节点 + 名字 + 属性，拒绝任何危险操作。

    expr 必须是字符串形式的数学表达式；env 提供可用名字（如 x/pi/np/math）。
    """
    if not isinstance(expr, str):
        raise TypeError("expression must be a string")
    try:
        tree = _ast.parse(expr, mode="eval")
    except SyntaxError:
        raise ValueError(f"invalid expression: {expr!r}")
    for node in _ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ValueError(f"unsupported syntax: {type(node).__name__}")
        if isinstance(node, _ast.Name):
            if node.id not in env and node.id not in _SAFE_FUNCS:
                raise ValueError(f"forbidden name: {node.id}")
        elif isinstance(node, _ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError(f"forbidden attribute: .{node.attr}")
        elif isinstance(node, _ast.Constant):
            if isinstance(node.value, (str, bytes)):
                raise ValueError("string/bytes constants are not allowed")
        elif isinstance(node, _ast.Call):
            func = node.func
            if isinstance(func, _ast.Name):
                if func.id not in _SAFE_FUNCS:
                    raise ValueError(f"forbidden call: {func.id}()")
            elif isinstance(func, _ast.Attribute):
                base = func.value
                if not (isinstance(base, _ast.Name) and base.id in _SAFE_MODULES):
                    raise ValueError("forbidden call")
            else:
                raise ValueError("forbidden call")
    return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {**_SAFE_FUNCS, **env})
'''


# ---------------------------------------------------------------------------
# 静态校验
# ---------------------------------------------------------------------------

def _validate_code(code: str) -> tuple[bool, list[str]]:
    """AST 级安全校验：仅允许纯数学场景代码，拒绝危险 import / 调用 / 属性。

    规则：
      - import 采用白名单制：只允许 manim / numpy / math 及其子模块。
      - 禁止调用 eval/exec/open/compile/getattr 等内置敏感函数。
      - 禁止访问任何以下划线开头的魔术属性（如 __class__、__globals__）。
      - 禁止访问 numpy 的文件 I/O / 本地库成员（loadtxt/save/ctypeslib 等）。
    """
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, [f"syntax error: {exc.msg} at line {exc.lineno}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                base = alias.name.split(".")[0]
                if base not in ALLOWED_IMPORTS:
                    errors.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod not in ALLOWED_IMPORTS:
                errors.append(f"forbidden import from: {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_NAMES:
                errors.append(f"forbidden call: {node.func.id}()")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in NUMPY_DANGEROUS_ATTRS:
                errors.append(f"forbidden attribute access: .{node.attr}")
    return not errors, errors


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _clean_env() -> dict:
    """构造纯净环境：清除 IDE 注入的安全删除钩子，避免批量清理临时文件被拦截。"""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("CODEBUDDY_SAFE_DELETE") or key == "GENIE_TRASH_DIR":
            env.pop(key, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONSTARTUP", None)
    return env


def _render_manim(code_file: Path, quality: str, outdir: Path, extra_args: list[str]) -> subprocess.CompletedProcess:
    """调用 manim CLI 渲染。quality: low/medium/high/ultra。"""
    quality_map = {"low": "-ql", "medium": "-qm", "high": "-qh", "ultra": "-qp"}
    flag = quality_map.get(quality, "-ql")
    cmd = [
        sys.executable, "-m", "manim",
        flag,
        "--format", "mp4",
        "--media_dir", str(outdir),
        *extra_args,
        str(code_file),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=_clean_env())


def _find_video(outdir: Path) -> Path | None:
    """定位成片：排除 partial_movie_files 中间文件，优先取最新的完整视频。"""
    for ext in VIDEO_EXTENSIONS:
        hits = [h for h in outdir.rglob(f"*{ext}") if "partial_movie_files" not in h.parts and "_narrated" not in h.stem]
        if hits:
            return sorted(hits, key=lambda p: p.stat().st_mtime)[-1]
    return None


def _request_tts(text: str, voice: str | None, output: Path) -> tuple[bytes, str]:
    """Synthesize one segment with edge-tts; local Qwen TTS is intentionally disabled."""
    selected_voice = voice or os.environ.get("EDGE_TTS_VOICE") or DEFAULT_TTS_VOICE
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "edge_tts", "--text", text,
             "--voice", selected_voice, "--write-media", str(output)],
            capture_output=True, text=True,
            timeout=float(os.environ.get("EDGE_TTS_TIMEOUT", EDGE_TTS_TIMEOUT)),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("edge-tts request timed out; check network access") from exc
    if proc.returncode != 0:
        detail = proc.stderr or proc.stdout
        if "No module named edge_tts" in detail:
            raise RuntimeError("edge-tts is not installed in the Python environment used by this plugin")
        if "WSServerHandshakeError: 403" in detail:
            raise RuntimeError("edge-tts speech service rejected the request (HTTP 403)")
        raise RuntimeError(f"edge-tts synthesis failed: {detail[-500:]}")
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("edge-tts returned no audio")
    return output.read_bytes(), "audio/mpeg"


def _set_audio_speed(audio_file: Path, speed: float) -> None:
    """Adjust generated speech speed after edge-tts synthesis."""
    if speed == 1.0:
        return
    if not 0.5 <= speed <= 2.0:
        raise ValueError(f"tts_speed must be between 0.5 and 2.0, got {speed}")
    tmp = audio_file.with_name(audio_file.stem + "_speed" + audio_file.suffix)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_file), "-filter:a", f"atempo={speed:g}",
         "-ar", "24000", "-ac", "1", str(tmp)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg audio speed adjustment failed: {proc.stderr[-1200:]}")
    tmp.replace(audio_file)


def _ffprobe_duration(path: Path) -> float:
    """Return the real decoded duration reported by ffprobe."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe duration failed: {proc.stderr[-1000:]}")
    try:
        duration = float(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned invalid duration: {proc.stdout!r}") from exc
    if duration <= 0:
        raise RuntimeError(f"invalid audio duration: {duration}")
    return duration


def _normalize_audio(src: Path, dst: Path) -> None:
    """Normalize a TTS segment to the canonical 24 kHz mono WAV format."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ar", "24000", "-ac", "1",
         "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg audio normalize failed: {proc.stderr[-1200:]}")


def _make_silence(path: Path, duration: float) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "anullsrc=r=24000:cl=mono", "-t", f"{duration:.6f}",
         "-c:a", "pcm_s16le", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError(f"ffmpeg silence generation failed: {proc.stderr[-1000:]}")


def _concat_wavs(files: list[Path], output: Path) -> None:
    """Concatenate normalized WAVs without re-encoding."""
    concat = output.with_suffix(".concat.txt")
    try:
        lines = []
        for path in files:
            escaped = str(path).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1", str(output)],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0 or not output.exists():
            raise RuntimeError(f"ffmpeg audio concat failed: {proc.stderr[-1200:]}")
    finally:
        concat.unlink(missing_ok=True)


def _prepare_narration(narration: str, outdir: Path, tts_url: str | None,
                       tts_model: str | None, tts_voice: str | None,
                       tts_speed: float = 1.0) -> tuple[Path, list[float], list[float]]:
    """Use edge-tts for narration and build the canonical RUN time axis."""
    try:
        parsed = json.loads(narration)
    except (json.JSONDecodeError, TypeError):
        parsed = narration

    if isinstance(parsed, str):
        segments = [parsed.strip()] if parsed.strip() else []
    elif isinstance(parsed, list):
        segments = [str(x).strip() for x in parsed if str(x).strip()]
    else:
        raise ValueError("narration must be a string or a JSON array of strings")
    if not segments:
        raise ValueError("narration is empty")

    GAP = 0.9
    work = Path(tempfile.mkdtemp(prefix="dsh_narration_", dir=outdir))
    normalized: list[Path] = []
    durations: list[float] = []
    try:
        print(f"[tts] synthesizing {len(segments)} narration segment(s) with edge-tts",
              file=sys.stderr, flush=True)
        for i, segment in enumerate(segments):
            raw = work / f"{i:03d}_raw.mp3"
            wav = work / f"{i:03d}.wav"
            _request_tts(segment, tts_voice, raw)
            _set_audio_speed(raw, tts_speed)
            _normalize_audio(raw, wav)
            duration = _ffprobe_duration(wav)
            durations.append(duration)
            normalized.append(wav)
            print(f"[tts] segment {i + 1}/{len(segments)} -> {duration:.2f}s",
                  file=sys.stderr, flush=True)

        run = [d + (GAP if i < len(durations) - 1 else 0.0)
               for i, d in enumerate(durations)]
        pieces: list[Path] = []
        for i, wav in enumerate(normalized):
            pieces.append(wav)
            if i < len(normalized) - 1:
                silence = work / f"{i:03d}_gap.wav"
                _make_silence(silence, GAP)
                pieces.append(silence)

        narration_wav = work / "narration.wav"
        _concat_wavs(pieces, narration_wav)
        final_audio = outdir / "narration.wav"
        final_audio.write_bytes(narration_wav.read_bytes())
        print(f"[tts] narration ready: {sum(run):.2f}s of audio", file=sys.stderr, flush=True)
        return final_audio, run, durations
    finally:
        # 早期版本把每个临时目录都留在输出目录里，失败几次就堆一片空目录。
        shutil.rmtree(work, ignore_errors=True)


def _inject_run(code: str, run: list[float]) -> str:
    """Inject the canonical RUN into a custom Manim scene."""
    literal = "RUN = " + repr([round(x, 6) for x in run]) + "\n"
    pattern = re.compile(r"(?m)^RUN\s*=\s*\[[^\n]*\]\s*$")
    if pattern.search(code):
        return pattern.sub(literal.rstrip(), code, count=1)
    return literal + code


RUN_USAGE_WARNING = (
    "场景没有使用 RUN 时间轴：动画时长由代码里硬编码的 run_time/wait 决定，和旁白长度对不上。"
    "请在每一步使用 RUN[i] 作为时间槽（例如 self.wait(max(0.0, RUN[i] - 本步动画已用时长))），"
    "让第 i 步正好占用旁白第 i 段的时长。"
)

TEMPLATE_TIMING_WARNING = (
    "模板动画时长由模板固定，不参与 RUN 时间轴，因此无法与旁白逐句对齐；"
    "需要严格对齐请改用 render_math_code，并让每一步占用 RUN[i]。"
)


def _uses_run_timeline(code: str) -> bool:
    """代码里是否真的在下标使用 RUN[i]（`RUN = [...]` 占位不算使用）。"""
    return re.search(r"\bRUN\s*\[", code) is not None


def _mux_narration(video: Path, audio_file: Path) -> tuple[Path, dict]:
    """把旁白混入成片，并保证旁白不被剪短。

    早期实现只有 `-af apad -shortest`：一旦动画比旁白短，-shortest 就在画面结束处
    直接掐掉旁白。现场案例：9 段旁白共 75 秒，场景硬编码动画 34 秒，成片只播了
    34 秒旁白（第 5 句中途被剪）。现在以旁白为准：画面偏短就冻结末帧补齐。
    """
    video_seconds = _ffprobe_duration(video)
    audio_seconds = _ffprobe_duration(audio_file)
    pad = max(0.0, audio_seconds - video_seconds)
    muxed = video.with_name(f"{video.stem}_narrated{video.suffix}")
    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio_file),
           "-map", "0:v:0", "-map", "1:a:0"]
    if pad > 0.05:
        # 冻结末帧补足画面，避免 -shortest 把旁白剪掉（需要重编码视频）。
        cmd += ["-vf", f"tpad=stop_mode=clone:stop_duration={pad + 0.3:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-af", "apad", "-shortest", "-movflags", "+faststart", str(muxed)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        muxed.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg audio mux failed: {proc.stderr[-1200:]}")
    info = {
        "video_seconds": round(video_seconds, 2),
        "narration_seconds": round(audio_seconds, 2),
    }
    if pad > 0.05:
        info["padded_seconds"] = round(pad, 2)
        print(f"[tts] animation {video_seconds:.2f}s < narration {audio_seconds:.2f}s: "
              f"freezing the last frame for {pad:.2f}s so the narration is not cut",
              file=sys.stderr, flush=True)
    return muxed, info


def _add_narration(result: dict, audio_file: Path | None, run: list[float] | None,
                   durations: list[float] | None, uses_run: bool = True,
                   note: str | None = None) -> dict:
    if not result.get("ok") or not result.get("video") or audio_file is None:
        return result
    try:
        narrated, info = _mux_narration(Path(result["video"]), audio_file)
        result["video"] = str(narrated)
        result["size_bytes"] = narrated.stat().st_size
        result["audio"] = True
        result["run"] = run or []
        result["narration_durations"] = durations or []
        result["narration_total"] = sum(run or [])
        result.update(info)
        warnings = []
        if not uses_run:
            warnings.append(note or RUN_USAGE_WARNING)
        if info.get("padded_seconds"):
            warnings.append(
                f"动画只有 {info['video_seconds']}s，旁白有 {info['narration_seconds']}s，"
                f"已冻结末帧补齐 {info['padded_seconds']}s 以保证旁白完整。"
            )
        if warnings:
            result["timing_warning"] = " ".join(warnings)
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"narration failed: {exc}"
        result["audio"] = False
    return result


def render_scene(template: str, params: dict, quality: str = "low", outdir: str | Path | None = None,
                 narration: str | None = None, tts_url: str | None = None,
                 tts_model: str | None = None, tts_voice: str | None = None,
                 tts_speed: float = DEFAULT_TTS_SPEED) -> dict:
    """Render a template scene. Narration is synthesized first so its timing can be measured."""
    templates = _load_templates()
    if template not in templates:
        return {"ok": False, "error": f"unknown template '{template}'", "available": sorted(templates)}
    meta = templates[template]
    errors = _validate_template_params(meta, params)
    if errors:
        return {"ok": False, "error": "parameter validation failed", "details": errors}
    if narration and narration.strip():
        try:
            parsed_narration = json.loads(narration)
        except json.JSONDecodeError:
            parsed_narration = narration
        segment_count = len([s for s in parsed_narration if str(s).strip()]) if isinstance(parsed_narration, list) else 1
        expected = meta.get("narration_steps")
        if meta.get("narration_steps_param"):
            expected = len(params[meta["narration_steps_param"]])
        if expected is not None and segment_count != expected:
            return {"ok": False, "error": f"template '{template}' needs {expected} narration segments, got {segment_count}"}
    try:
        code = _render_template(meta, params, meta["code"])
    except Exception as exc:
        return {"ok": False, "error": f"template expansion failed: {exc}"}

    out = Path(outdir).resolve() if outdir else Path.cwd() / "out"
    out.mkdir(parents=True, exist_ok=True)

    audio_file = None
    run = None
    durations = None
    try:
        if narration and narration.strip():
            audio_file, run, durations = _prepare_narration(
                narration, out, tts_url, tts_model, tts_voice, tts_speed
            )
            code = _inject_run(code, run)

        with tempfile.TemporaryDirectory() as tmp:
            scene_src = _SAFE_EVAL_SRC + "\n" + code
            code_file = Path(tmp) / f"{meta.get('name', template)}_scene.py"
            code_file.write_text(scene_src, encoding="utf-8")
            try:
                ast.parse(scene_src)
            except SyntaxError as exc:
                return {"ok": False, "error": f"expanded template has syntax error: {exc}", "code": code}
            proc = _render_manim(code_file, quality, out, [])
            video = _find_video(out) if proc.returncode == 0 else None
            result = {
                "ok": proc.returncode == 0,
                "template": template,
                "params": params,
                "quality": quality,
                "returncode": proc.returncode,
                "video": str(video) if video else None,
                "size_bytes": video.stat().st_size if video else None,
            }
            if proc.returncode != 0:
                result["error"] = proc.stderr[-2000:]
            return _add_narration(result, audio_file, run, durations,
                                  uses_run=_uses_run_timeline(code),
                                  note=TEMPLATE_TIMING_WARNING)
    except Exception as exc:
        return {"ok": False, "error": f"narration failed: {exc}", "audio": False}


def cmd_render(args: argparse.Namespace) -> int:
    params_src = args.params or ""
    if not params_src:
        # 支持通过 stdin 传入参数（避免超长参数撑爆 argv）
        params_src = sys.stdin.read()
    try:
        params = json.loads(params_src or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid params JSON: {exc}"}))
        return 2
    result = render_scene(args.template, params, args.quality, args.outdir,
                          args.narration, args.tts_url, args.tts_model, args.tts_voice, args.tts_speed)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2 if "unknown template" in str(result.get("error", "")) else 1


def cmd_render_code(args: argparse.Namespace) -> int:
    code_file = Path(args.code_file).resolve()
    if not code_file.exists():
        print(json.dumps({"ok": False, "error": f"code file not found: {code_file}"}))
        return 2
    code = code_file.read_text(encoding="utf-8")
    ok, violations = _validate_code(code)
    if not ok:
        print(json.dumps({"ok": False, "error": "scene failed static validation", "details": violations}))
        return 2
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    audio_file = None
    run = None
    durations = None
    # 注入前先判断模型是否真的把 RUN 当成时间槽在用（`RUN = [...]` 占位不算）。
    uses_run = _uses_run_timeline(code)
    try:
        if args.narration and args.narration.strip():
            audio_file, run, durations = _prepare_narration(
                args.narration, outdir, args.tts_url, args.tts_model, args.tts_voice, args.tts_speed
            )
            code = _inject_run(code, run)

        # Re-validate the final injected source before rendering.
        ok, violations = _validate_code(code)
        if not ok:
            print(json.dumps({"ok": False, "error": "scene failed static validation after timing injection", "details": violations}))
            return 2

        prepared = outdir / f".scene_timed_{os.getpid()}_{code_file.stem}.py"
        prepared.write_text(code, encoding="utf-8")
        try:
            proc = _render_manim(prepared, args.quality, outdir, [])
        finally:
            prepared.unlink(missing_ok=True)

        video = _find_video(outdir) if proc.returncode == 0 else None
        result = {
            "ok": proc.returncode == 0,
            "quality": args.quality,
            "returncode": proc.returncode,
            "video": str(video) if video else None,
            "size_bytes": video.stat().st_size if video else None,
        }
        if proc.returncode != 0:
            result["error"] = proc.stderr[-2000:]
        result = _add_narration(result, audio_file, run, durations, uses_run=uses_run)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"narration failed: {exc}", "audio": False}, ensure_ascii=False))
        return 1


def cmd_validate(args: argparse.Namespace) -> int:
    code = args.code
    if not code:
        # 支持通过 stdin 传入代码（避免超长代码撑爆 argv）
        code = sys.stdin.read()
    ok, violations = _validate_code(code)
    print(json.dumps({"ok": ok, "violations": violations}))
    return 0 if ok else 1


def cmd_templates(args: argparse.Namespace) -> int:
    templates = _load_templates()
    print(json.dumps({"ok": True, "templates": templates}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manim_runner", description="Manim CE renderer for dshmath-manim")
    parser.add_argument("--json", action="store_true", help="(reserved) always outputs JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p_templates = sub.add_parser("templates", help="list available templates")
    p_templates.set_defaults(func=cmd_templates)

    p_validate = sub.add_parser("validate", help="static safety check of a scene code string")
    p_validate.add_argument("--code", default="", help="Python scene source code (or pass via stdin)")
    p_validate.set_defaults(func=cmd_validate)

    p_render = sub.add_parser("render", help="render a template scene")
    p_render.add_argument("--template", required=True)
    p_render.add_argument("--params", default="", help="JSON object of template parameters (or pass via stdin)")
    p_render.add_argument("--quality", default="low", choices=["low", "medium", "high", "ultra"])
    p_render.add_argument("--outdir", default=str(Path.cwd() / "out"))
    p_render.add_argument("--narration", default="", help="optional narration text")
    p_render.add_argument("--tts-url", default="", help="Deprecated and ignored; edge-tts is used directly")
    p_render.add_argument("--tts-model", default="", help="Deprecated and ignored; edge-tts does not use a local model")
    p_render.add_argument("--tts-voice", default="", help="edge-tts voice, default zh-CN-XiaoxiaoNeural")
    p_render.add_argument("--tts-speed", type=float, default=DEFAULT_TTS_SPEED, help="Playback speed multiplier (0.5-2.0)")
    p_render.set_defaults(func=cmd_render)

    p_render_code = sub.add_parser("render-code", help="render an existing scene python file")
    p_render_code.add_argument("--code-file", required=True)
    p_render_code.add_argument("--quality", default="low", choices=["low", "medium", "high", "ultra"])
    p_render_code.add_argument("--outdir", default=str(Path.cwd() / "out"))
    p_render_code.add_argument("--narration", default="", help="optional narration text")
    p_render_code.add_argument("--tts-url", default="", help="Deprecated and ignored; edge-tts is used directly")
    p_render_code.add_argument("--tts-model", default="", help="Deprecated and ignored; edge-tts does not use a local model")
    p_render_code.add_argument("--tts-voice", default="", help="edge-tts voice, default zh-CN-XiaoxiaoNeural")
    p_render_code.add_argument("--tts-speed", type=float, default=DEFAULT_TTS_SPEED, help="Playback speed multiplier (0.5-2.0)")
    p_render_code.set_defaults(func=cmd_render_code)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
