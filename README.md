# dshmath-manim

**dshmath-manim** 是一个面向 [DeepSeek Harness（DSH）](https://github.com/cfxing/dshmath-manim) 的 Manim CE 数学与物理动画插件。用户用自然语言描述想学习的概念，Agent 通过插件工具和技能选择模板或编写 Manim 场景，生成动画视频；需要讲解时，还可以使用本地 Qwen3-TTS 生成中文旁白并与视频合成。

- **零代码使用**：用户不需要编写 Python 或 Manim 代码。
- **模板优先**：常见数学可视化通过参数化模板生成。
- **自由代码补充**：模板无法表达复杂分镜、运动或物理过程时，切换到 Manim 场景代码路径。
- **中文旁白**：支持 Qwen3-TTS 本地合成、分段旁白、音频时长测量和视频合成。
- **安全校验**：渲染前对自由场景代码进行 AST 静态检查。

## 功能一览

| 能力 | 说明 |
|---|---|
| 函数图像 | 绘制一条或多条函数曲线 |
| 导数与切线 | 展示切线、切点及数值斜率 |
| 定积分 | 展示曲线与坐标轴围成的面积 |
| 平面几何 | 三角形、正方形、圆和正多边形等 |
| 极坐标 | 心形线、玫瑰线、螺旋等 |
| 三维曲面 | 绘制并观察 (z=f(x,y)) 曲面 |
| 自定义动画 | 通过 Manim CE 场景表达多步动画、物体运动和物理过程 |
| 中文旁白 | 使用本地 Qwen3-TTS 生成音频，并与 MP4 合成 |

## 工作流程

### 动画生成

```text
用户的自然语言需求
        ↓
math-animation 技能
        ↓
列出模板并选择合适的动画类型
        ↓
render_math_scene ── 模板能够表达时优先使用
        │
        └── 模板无法表达时使用 manim-codegen
                         ↓
                 静态校验与 Manim 渲染
                         ↓
                       MP4
```

### 带旁白的视频

带讲解的视频使用分段旁白，以便让音频与动画步骤对应：

```text
narration_segments
        ↓
Qwen3-TTS 逐段生成 WAV
        ↓
FFprobe 测量每段音频的实际时长
        ↓
在段落之间加入 0.9 秒间隔
        ↓
根据实际时长计算 RUN 时间轴
        ↓
Manim 按 RUN 安排动画步骤
        ↓
FFmpeg 将旁白与视频合成
```

时间轴应由生成后的真实音频时长计算，不应由 Agent 凭感觉填写。自由 Manim 场景可以使用全局 `RUN[i]` 表示第 i 个步骤的时间槽；渲染器会在支持的场景代码中注入或替换 RUN。

## TTS 默认设置

| 设置 | 默认值 | 说明 |
|---|---|---|
| 模型 | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | 本地 Qwen3-TTS CustomVoice 模型 |
| 音色 | `Serena` | 所有旁白段统一使用同一音色 |
| 语速 | `0.97` | 合成后由 FFmpeg `atempo` 调整，约比正常速度慢 3% |
| 采样率 | `24000 Hz` | 规范化后的旁白音频格式 |
| 段间间隔 | `0.9 秒` | 用于在相邻讲解步骤之间留出停顿 |

Qwen3-TTS 的 `generate_custom_voice()` 不接收 `speed` 或 `rate` 参数。插件的 `tts_speed` 是生成后的音频处理设置，不会作为模型参数传入。

> 语音合成需要本机安装并可运行 Qwen3-TTS 模型。若模型首次加载较慢，请先在目标 Conda 环境中单独验证模型和 GPU 推理。

## 插件工具

插件通过 Cordis 向 DSH 注册以下工具：

| 工具 | 用途 |
|---|---|
| `list_math_templates` | 查询可用模板及参数定义 |
| `render_math_scene` | 使用已定义模板和参数生成动画 |
| `render_math_code` | 校验并渲染自定义 Manim 场景 |
| `validate_math_code` | 对自定义场景进行 AST 静态安全检查 |

渲染工具可接收讲稿；需要逐步同步动画时，优先提供 `narration_segments`，每个元素对应一个动画步骤。

## 技能（Skills）

插件包含两个技能：

| 技能 | 路径 | 适用场景 |
|---|---|---|
| `math-animation` | `skills/math-animation/SKILL.md` | **默认路径**：把自然语言需求翻译为模板与参数 |
| `manim-codegen` | `skills/manim-codegen/SKILL.md` | **进阶路径**：模板无法覆盖时生成自定义 Manim 场景 |

技能采用模板优先策略。Agent 会先判断模板能否表达用户需求；只有模板不适用或渲染反复失败时，才切换到自由代码路径。用户始终可以使用自然语言描述需求。

## 示例

在 DSH 对话中，可以直接提出类似需求：

- “绘制正弦函数和余弦函数在同一坐标系中的对比动画。”
- “展示 (y=x^2) 在 (x=1) 处的切线，并讲解导数的含义。”
- “用动画演示 (sin x) 从 (0) 到 (pi) 的定积分面积。”
- “画一个三角形并标注角 ABC。”
- “绘制极坐标心形线 (r=1+\cos\theta)。”
- “展示马鞍面 (z=x^2-y^2)。”
- “制作弹簧振子的运动动画，并配上中文讲解。”

## 环境要求

- DeepSeek Harness（DSH）及兼容的插件运行环境
- Node.js 与 pnpm/npm，用于安装和构建 TypeScript 插件
- Python 3
- Manim Community Edition（CE）
- NumPy
- FFmpeg（提供 `ffmpeg` 和 `ffprobe`）
- 需要中文旁白时：本地 `qwen-tts` Python 包、可用的 Qwen3-TTS 模型及兼容的 PyTorch/CUDA 环境

建议将 DSH 插件运行环境与 Qwen3-TTS 的 Conda 环境分开管理。插件可以通过配置的 Python 解释器调用 Qwen3-TTS 环境；FFmpeg/FFprobe 可使用系统安装版本。请确保运行插件的进程能够找到所需的可执行文件。

## 安装与运行

### 1. 安装 Python 渲染依赖

在用于 Manim 渲染的 Python 环境中安装：

```bash
python3 -m pip install manim numpy
```

如需 TTS，请在专门的 Qwen3-TTS 环境中按该模型及项目的安装说明安装 `qwen-tts`，并确认模型能够正常加载。若使用 NVIDIA GPU，PyTorch、CUDA 与显卡驱动版本需要相互兼容。

Ubuntu 可安装 FFmpeg 工具：

```bash
sudo apt update
sudo apt install ffmpeg
```

确认命令可用：

```bash
which ffmpeg
which ffprobe
ffmpeg -version
ffprobe -version
```

### 2. 安装 JavaScript 依赖并构建

```bash
git clone https://github.com/cfxing/dshmath-manim.git
cd dshmath-manim
pnpm install
pnpm build
```

### 3. 在 DSH 中加载插件

在 DSH 仓库或相应开发环境中，通过插件配置加载本项目的 `math-manim.cordis.yml`。例如：

```bash
dsh --patch /path/to/dshmath-manim/math-manim.cordis.yml
```

请根据本机 DSH 的安装方式调整路径和启动命令。当前 `package.json` 声明的 DSH peer 依赖版本为 `^0.2.0-rc.2`。

## 不依赖 DSH 的后端验证

可先直接调用 Python 渲染器，验证模板系统和 Manim 环境。

列出模板：

```bash
python3 py/manim_runner.py templates
```

渲染函数图像示例：

```bash
python3 py/manim_runner.py render \
  --template function_plot \
  --params '{"functions":["np.sin(x)","x**2"],"title":"y=sin(x), y=x^2"}' \
  --quality low \
  --outdir out
```

测试静态安全校验：

```bash
python3 py/manim_runner.py validate --code 'import os; os.system("echo unsafe")'
```

该示例用于确认危险导入或调用会被校验器拒绝，不应将不可信的场景代码绕过校验后执行。

## 配置与故障排查

### Qwen3-TTS

插件默认使用本地 Qwen3-TTS；只有在确实部署了兼容的 HTTP TTS 服务时，才配置 `QWEN3_TTS_URL` 切换到 HTTP 路径。可根据部署情况配置模型、音色、语言、设备和数据类型等环境变量；具体以当前后端代码支持的变量为准。

如果 TTS 失败，建议按顺序检查：

1. 使用插件实际调用的 Python 解释器直接运行一次短句合成。
2. 确认模型文件可访问，且模型与 CUDA/PyTorch 环境兼容。
3. 确认输出 WAV 文件生成成功且非空。
4. 使用 `ffprobe` 检查音频时长。
5. 查看插件返回的 stderr 或异常详情，区分模型加载、推理和音频合成阶段的问题。

首次启动模型会产生加载开销。分段旁白的时间轴以各段实际音频时长为依据；不要通过手工猜测 RUN 或只按文本长度估算时长来掩盖同步问题。

### Manim 与 FFmpeg

- 找不到 `manim`：确认它安装在渲染器实际使用的 Python 环境中。
- 找不到 `ffmpeg` 或 `ffprobe`：检查系统 PATH，并确保启动 DSH 的进程继承了正确环境。
- 中文文本显示异常：检查 Manim、Pango 和系统中文字体是否可用。
- LaTeX 渲染失败：先尝试简化标题或改用普通文本，再检查 TeX 环境。

## 项目结构

```text
dshmath-manim/
├── math-manim.cordis.yml       # DSH 插件加载配置
├── package.json                # 包元数据、依赖与构建脚本
├── src/
│   ├── index.ts                # 插件入口与工具注册
│   └── runner.ts               # TypeScript 与 Python 后端桥接
├── py/
│   ├── manim_runner.py         # 模板、校验、渲染与音视频处理
│   ├── qwen3_tts_runner.py     # 本地 Qwen3-TTS 适配器
│   ├── templates/              # 模板定义
│   └── examples/               # 自定义场景示例
└── skills/
    ├── math-animation/SKILL.md # 模板优先的数学动画技能
    └── manim-codegen/SKILL.md # 自由 Manim 场景生成技能
```

## 安全设计

- **模板路径**：使用已声明的模板和参数进行渲染，避免直接执行任意模型表达式作为 Python 代码。
- **自由代码路径**：渲染前执行 AST 静态校验，拦截已列入规则的危险导入、名称与调用。
- **子进程执行**：Python 渲染通过独立子进程运行，并设置超时与取消处理。
- **边界说明**：AST 静态检查是防护措施之一，不等同于完整操作系统沙箱；不要将其视为对任意不可信代码的绝对安全保证。

## 构建与打包

```bash
pnpm build
npm pack
```

打包内容由 `package.json` 的 `files` 字段控制，包含编译产物、Python 后端、技能文件、插件配置和 README。发布或分发前，建议在目标 DSH profile 中验证安装、工具注册、Manim 渲染和 TTS 合成的完整流程。

## 开发状态

本项目持续迭代中。涉及模型加载复用、TTS 性能、UI 预览和 DSH 集成的行为，应以当前分支代码和实际运行结果为准。欢迎通过 Issues 或 Pull Requests 反馈问题。
