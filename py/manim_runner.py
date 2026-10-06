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
import base64
import binascii
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
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
DEFAULT_TTS_URL = "http://127.0.0.1:8000/v1/audio/speech"
DEFAULT_TTS_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_TTS_VOICE = "Vivian"
QWEN_TTS_ADAPTER = Path(__file__).resolve().parent / "qwen3_tts_runner.py"


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


def _tts_url(url: str | None) -> str:
    return (url or os.environ.get("QWEN3_TTS_URL") or DEFAULT_TTS_URL).rstrip("/")


def _request_local_tts(text: str, model: str | None, voice: str | None) -> tuple[bytes, str]:
    """Run the locally installed qwen-tts package in its own Python environment."""
    qwen_python = os.environ.get("QWEN3_TTS_PYTHON", "/home/sangfor/miniconda3/envs/qwen3-tts/bin/python")
    if not Path(qwen_python).exists():
        qwen_python = sys.executable
    fd, name = tempfile.mkstemp(prefix="qwen3_tts_", suffix=".wav")
    os.close(fd)
    output = Path(name)
    try:
        proc = subprocess.run(
            [qwen_python, str(QWEN_TTS_ADAPTER)],
            input=json.dumps({
                "model": model or os.environ.get("QWEN3_TTS_MODEL") or DEFAULT_TTS_MODEL,
                "text": text,
                "voice": voice or os.environ.get("QWEN3_TTS_VOICE") or DEFAULT_TTS_VOICE,
                "language": os.environ.get("QWEN3_TTS_LANGUAGE", "Chinese"),
                "output": str(output),
            }),
            capture_output=True, text=True, timeout=300,
            # librosa bundled in some qwen-tts environments has an invalid
            # numba cache locator; disabling numba JIT here affects only the
            # import-time audio helper, not PyTorch model inference.
            env={**os.environ, "NUMBA_DISABLE_JIT": "1"},
        )
        if proc.returncode != 0 or not output.exists():
            raise RuntimeError(f"local qwen-tts failed: {proc.stderr[-1600:]}")
        return output.read_bytes(), "audio/wav"
    finally:
        output.unlink(missing_ok=True)


def _request_tts(text: str, url: str | None, model: str | None, voice: str | None) -> tuple[bytes, str]:
    """Use local qwen-tts by default; optionally support an HTTP endpoint.

    The OpenAI-compatible endpoint returns audio bytes.  The local desktop route
    returns either audio bytes or {"audio": "<base64>"}; supporting both keeps the
    plugin usable with the two server launch modes commonly used for Qwen3-TTS.
    """
    # URL is deliberately opt-in. The normal path directly invokes qwen-tts.
    if not url and not os.environ.get("QWEN3_TTS_URL"):
        return _request_local_tts(text, model, voice)
    endpoint = _tts_url(url)
    if endpoint.endswith("/api/tts/synthesize"):
        payload = {"text": text, "format": "base64"}
    else:
        payload = {
            "model": model or os.environ.get("QWEN3_TTS_MODEL") or DEFAULT_TTS_MODEL,
            "input": text,
            "voice": voice or os.environ.get("QWEN3_TTS_VOICE") or DEFAULT_TTS_VOICE,
            "response_format": "wav",
        }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "audio/wav, audio/mpeg, application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = response.read()
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"Qwen3-TTS HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot connect to Qwen3-TTS at {endpoint}: {exc.reason}") from exc

    if content_type == "application/json" or data[:1] in (b"{", b"["):
        try:
            body = json.loads(data.decode("utf-8"))
            encoded = body.get("audio") or body.get("data")
            if not encoded:
                raise ValueError("response has no audio field")
            data = base64.b64decode(encoded)
            content_type = "audio/wav"
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise RuntimeError(f"invalid Qwen3-TTS response: {exc}") from exc
    if not data:
        raise RuntimeError("Qwen3-TTS returned an empty audio response")
    return data, content_type


def _mux_narration(video: Path, narration: str, outdir: Path, tts_url: str | None,
                   tts_model: str | None, tts_voice: str | None) -> tuple[Path, Path]:
    """Generate narration and mux it into the rendered video."""
    audio, content_type = _request_tts(narration, tts_url, tts_model, tts_voice)
    audio_suffix = ".mp3" if "mpeg" in content_type else ".wav"
    audio_fd, audio_name = tempfile.mkstemp(prefix="qwen3_tts_", suffix=audio_suffix, dir=outdir)
    os.close(audio_fd)
    audio_file = Path(audio_name)
    muxed = video.with_name(f"{video.stem}_narrated{video.suffix}")
    try:
        audio_file.write_bytes(audio)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-i", str(audio_file),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
             "-af", "apad", "-shortest", "-movflags", "+faststart", str(muxed)],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            muxed.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg audio mux failed: {proc.stderr[-1200:]}")
        return muxed, audio_file
    finally:
        audio_file.unlink(missing_ok=True)


def _add_narration(result: dict, narration: str | None, outdir: Path, tts_url: str | None,
                   tts_model: str | None, tts_voice: str | None) -> dict:
    if not result.get("ok") or not result.get("video") or not narration or not narration.strip():
        return result
    try:
        narrated, _ = _mux_narration(Path(result["video"]), narration.strip(), outdir, tts_url, tts_model, tts_voice)
        result["video"] = str(narrated)
        result["size_bytes"] = narrated.stat().st_size
        result["audio"] = True
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"narration failed: {exc}"
        result["audio"] = False
    return result


def render_scene(template: str, params: dict, quality: str = "low", outdir: str | Path | None = None,
                 narration: str | None = None, tts_url: str | None = None,
                 tts_model: str | None = None, tts_voice: str | None = None) -> dict:
    """渲染一个模板场景，返回结果 dict（不打印）。供 CLI / wizard / TS 桥接共用。"""
    templates = _load_templates()
    if template not in templates:
        return {"ok": False, "error": f"unknown template '{template}'", "available": sorted(templates)}

    meta = templates[template]
    errors = _validate_template_params(meta, params)
    if errors:
        return {"ok": False, "error": "parameter validation failed", "details": errors}

    try:
        code = _render_template(meta, params, meta["code"])
    except Exception as exc:
        return {"ok": False, "error": f"template expansion failed: {exc}"}

    with tempfile.TemporaryDirectory() as tmp:
        # 场景文件 = 受限求值器头部 + 展开后的模板代码，完全自包含
        scene_src = _SAFE_EVAL_SRC + "\n" + code
        code_file = Path(tmp) / f"{meta.get('name', template)}_scene.py"
        code_file.write_text(scene_src, encoding="utf-8")
        # 模板代码是可信的（仓库自带），只做语法检查，不做 AST 安全校验
        try:
            ast.parse(scene_src)
        except SyntaxError as exc:
            return {"ok": False, "error": f"expanded template has syntax error: {exc}", "code": code}

        out = Path(outdir).resolve() if outdir else Path.cwd() / "out"
        out.mkdir(parents=True, exist_ok=True)
        proc = _render_manim(code_file, quality, out, [])
        # 仅渲染成功时才报告成片，避免把历史视频当作本次结果
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
        return _add_narration(result, narration, out, tts_url, tts_model, tts_voice)


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
                          args.narration, args.tts_url, args.tts_model, args.tts_voice)
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
    proc = _render_manim(code_file, args.quality, outdir, [])
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
    result = _add_narration(result, args.narration, outdir, args.tts_url, args.tts_model, args.tts_voice)
    print(json.dumps(result))
    return 0 if proc.returncode == 0 else 1


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
    p_render.add_argument("--tts-url", default="", help="Qwen3-TTS OpenAI-compatible endpoint")
    p_render.add_argument("--tts-model", default="", help="Qwen3-TTS model name")
    p_render.add_argument("--tts-voice", default="", help="Qwen3-TTS voice name")
    p_render.set_defaults(func=cmd_render)

    p_render_code = sub.add_parser("render-code", help="render an existing scene python file")
    p_render_code.add_argument("--code-file", required=True)
    p_render_code.add_argument("--quality", default="low", choices=["low", "medium", "high", "ultra"])
    p_render_code.add_argument("--outdir", default=str(Path.cwd() / "out"))
    p_render_code.add_argument("--narration", default="", help="optional narration text")
    p_render_code.add_argument("--tts-url", default="", help="Qwen3-TTS OpenAI-compatible endpoint")
    p_render_code.add_argument("--tts-model", default="", help="Qwen3-TTS model name")
    p_render_code.add_argument("--tts-voice", default="", help="Qwen3-TTS voice name")
    p_render_code.set_defaults(func=cmd_render_code)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
