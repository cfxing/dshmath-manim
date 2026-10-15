# dshmath-manim

DeepSeek Harness（dsh）学习动画插件 —— 基于 Manim CE 将数学、物理、生物、化学等知识渲染为视频，并可通过 edge-tts 自动添加晓晓旁白。

「一切皆插件」：本插件以 Cordis 插件形式提供一组 **Tool**，模型通过自然语言即可生成教学动画。

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

```
dshmath-manim/
├── math-manim.cordis.yml     # 插件加载配置
├── package.json / tsconfig   # TS 插件（tools 注册）
├── src/
│   ├── index.ts              # 插件入口，注册 4 个工具
│   └── runner.ts             # TS ⇄ Python 桥接（子进程 + 超时 + 安全环境）
└── py/
    ├── manim_runner.py       # 渲染执行器（模板/校验/渲染，JSON 输出）
    ├── templates/            # 教学动画模板
    │   ├── function_plot     # 函数绘图（一/二元函数）
    │   ├── derivative_tangent# 导数与切线（数值斜率）
    │   ├── definite_integral # 定积分（曲线下面积）
    │   ├── geometry          # 几何图形（三角形/圆/正多边形）
    │   ├── polar_plot        # 极坐标曲线（心形线等）
    │   ├── surface_3d        # 3D 曲面（ThreeDScene）
    │   ├── cell_structure    # 生物：动物/植物细胞结构
    │   ├── chemical_reaction # 化学：反应物到生成物
    │   └── learning_process  # 通用：2～5 步知识讲解
    └── examples/             # 自定义场景示例（走 render_math_code 自由代码路径）
        ├── spring_mass_oscillator.py  # 弹簧振荡（简谐运动，含 x(t) 轨迹同步）
        ├── spring_oscillator.py       # 弹簧振子（位移-时间）
        └── bouncing_ball.py           # 小球弹跳
```

## 教学动画向导（零代码使用）

面向不懂代码的用户。启动后浏览器打开即可使用：

```bash
python3 -m pip install manim numpy
```

流程：选择场景卡片（包括细胞结构、化学反应、分步讲解）→ 填写参数（全中文表单）→ 点击「生成动画」→ 预览并下载视频。全程无任何代码。

每个模板在 `py/templates/*.json` 中带有 `ui` 元数据（中文标签、参数提示、控件类型、下拉选项），向导页面由此自动生成表单。

## 提供的工具（模型可见）

| 工具 | 作用 | 适用场景 |
|---|---|---|
| `list_math_templates` | 列出模板与参数模式 | 开场发现能力 |
| `render_math_scene` | 模板参数化渲染（安全） | 推荐路径 |
| `render_math_code` | 渲染模型自写 Manim 场景 | 模板不匹配时 |
| `validate_math_code` | AST 静态安全校验 | 渲染前的自愈检查 |

两个渲染工具都支持分段旁白。传入中文讲稿后，插件通过 `edge-tts` 使用
`zh-CN-XiaoxiaoNeural`（晓晓）生成语音，再用 ffmpeg 合成 MP4。请先安装
`edge-tts`（例如 `pip install -r requirements.txt`）。生物、化学和通用模板会
按旁白段数安排画面时长：细胞结构和化学反应需要 3 段，通用模板每个步骤 1 段。

生物、化学以示意图和流程动画为主；需要精细结构或实验过程时，学习动画技能会
改用自定义 Manim 场景。化学式、条件与配平系数须先核对。

## 零代码技能包（Skill，核心推荐）

插件加载时会自动向 `ctx.skills` 注册三个技能（`skills/<name>/SKILL.md`）：

| 技能 | 路径 | 定位 |
|---|---|---|
| `math-animation` | `skills/math-animation/` | **默认推荐**：模板路径。把模型训练成"数学动画翻译官"，口语 → 模板参数 → `render_math_scene` |
| `learning-animation` | `skills/learning-animation/` | **用户入口**：根据科目选择模板或自定义场景，默认带中文旁白 |
| `manim-codegen` | `skills/manim-codegen/` | **进阶**：模板无法表达时，编写 Manim 场景 → `validate_math_code` → `render_math_code` |

**双轨自动决策**：`math-animation` 技能内置决策树，模板覆盖得了的需求走模板（安全、成功率高）；覆盖不了或连续失败时，自动加载 `manim-codegen` 切换自由代码。用户视角始终是零代码。

### 使用者视角（零代码，完全不用接触代码）

在 dsh 对话中直接说学习需求即可，例如：

- 「画一个正弦函数和余弦函数在同一坐标系里的对比动画」
- 「y=x² 在 x=1 处的切线，标出导数」
- 「画出 sin x 从 0 到 π 的定积分，涂上面积」
- 「画一个蓝色三角形，标 ∠ABC」
- 「画心形线 r=1+cosθ」
- 「画马鞍面 z=x²-y²，能旋转的」
- 「弹簧振子的位移-时间图像」（物理 → 自动翻译为简谐运动曲线）
- 「做一个动物细胞结构讲解视频」
- 「讲解 2H₂ + O₂ → 2H₂O 的反应过程」
- 「用动画解释光合作用的主要步骤」

模型会加载技能 → 翻译成模板参数 → 调用 `render_math_scene` → 返回视频。超出模板时自动切换 `manim-codegen` 写代码渲染。技能内置：

- **口语 → 表达式翻译表**（"x 的平方"→`x**2`，极坐标、3D 双变量同样覆盖）
- **模板选择决策树**（需求类型 → 模板）
- **精细动画加分项**（标题智能渲染：数学公式走 LaTeX、含中文自动走 Pango 文本渲染，中文标题无忧；贴合函数形状的坐标区间、多曲线配色、low 预览→high 出片）
- **自愈循环**（参数校验/表达式/LaTeX 报错 → 自动修正重试，连续 2 次失败才询问用户）

### 技能启用方式（二选一）

```bash
dsh --patch /path/to/dshmath-manim/math-manim.cordis.yml
```

请根据本机 DSH 的安装方式调整路径和启动命令。当前 `package.json` 声明的 DSH peer 依赖版本为 `^0.2.0-rc.2`。

## 不依赖 DSH 的后端验证

可先直接调用 Python 渲染器，验证模板系统和 Manim 环境。

列出模板：

```bash
pip install manim numpy -r requirements.txt
```

### 2. 独立验证 Python 后端（无需 dsh）

```bash
# 列出模板
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

### 方式三：GitHub 安装

给仓库打 `dsh-plugin` 话题即可进入社区生态；使用者可 `dsh plugin add github:you/dshmath-manim`。
注意 git 安装拉取的是源码，需要 `prepare` 脚本且用户需在 profile 的 `pnpm-workspace.yaml`
`allowBuilds` 中授权构建——推荐优先用 npm / tarball 避免此门槛。

### 使用者安装后的效果

安装成功后，使用者只需在 dsh 对话中描述知识点，例如「画一个蓝色三角形标 ∠ABC」或
「弹簧振子的动画」，模型会加载插件注册的两个技能（`math-animation` 模板路径 / `manim-codegen`
自由代码路径）自动生成动画。**使用者侧无需安装任何 Python 依赖** —— 但渲染需要本机已安装
`manim` + `numpy`（见上「Python 依赖」），或后续可内置自动检测提示。

## 安全模型

- **模板渲染**：参数只做 `{key}` 文本替换 + JSON 字面量序列化，**绝不 eval 模型输入**
- **模型代码**：AST 静态校验，拦截 `os`/`subprocess`/`eval`/`open` 等危险符号后才渲染
- **进程隔离**：渲染始终在 Python 子进程内执行，带超时与取消信号（`exec.signal`）
- **环境隔离**：子进程清除 IDE 注入的钩子（`CODEBUDDY_SAFE_DELETE_*`），避免 TeX 临时文件清理被拦截

## 自愈循环（与 dsh agent loop 协同）

模型写出场景 → `validate_math_code` 检查 → 失败则根据 violations 重写 → `render_math_scene`/`render_math_code` 渲染 → 渲染错误（含 LaTeX 报错）作为 tool result 回喂 → 模型修正。错误信息被裁剪到末尾 2000 字符返回，避免上下文爆炸。

## 路线图

- [x] Python 渲染后端 + 6 个数学模板（本地验证通过）
- [x] TS 插件（4 个工具注册）
- [x] 零代码技能包：`math-animation` Skill（模板路径：提示词引导参数翻译）
- [x] 进阶技能包：`manim-codegen` Skill（自由代码路径：模型直接写 Manim 场景代码）
- [ ] dsh 仓库内集成测试（需在 deepseek-harness 仓库中 pnpm 安装验证）
- [ ] UI 卡片（`presentResult` 内嵌视频预览）
- [ ] 对话式增量修改（改模板参数重渲染）
