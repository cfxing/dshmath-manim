## 项目概述
dshmath-manim：DeepSeek Harness（dsh）的数学动画插件。以 Cordis 插件形式向模型注册一组 Tool，模型通过自然语言生成数学动画，底层由 Python 子进程调用 Manim CE 完成渲染。核心交付是插件/库，非独立服务。

## 技术栈
- TypeScript（NodeNext, ESM, tsc 构建），Cordis 插件框架（@deepseek-ai/cordis）
- Python 3.12 渲染执行器（manim_runner.py）
- 零代码 Web 向导：py/wizard_server.py（仅开发辅助，非主产物）

## 目录结构
```
├── math-manim.cordis.yml     # 插件加载配置
├── package.json / tsconfig   # TS 插件（tools 注册）
├── src/
│   ├── index.ts              # 插件入口，注册 Tool
│   ├── runner.ts             # TS ⇄ Python 桥接（子进程+超时+安全环境）
│   └── skill.ts
├── py/
│   ├── manim_runner.py       # 渲染执行器（JSON 输出）
│   ├── templates/            # 数学动画模板（JSON 声明参数 + Python 场景）
│   ├── examples/             # 自由代码路径示例场景
│   └── wizard_server.py      # 零代码 Web 向导（开发辅助）
└── skills/                   # 注册到 ctx.skills 的技能包（每份含 SKILL.md）
```

## 关键入口 / 核心模块
- src/index.ts：插件入口，注册 `list_math_templates` / `render_math_scene` / `render_math_code` / `validate_math_code` 四个 Tool
- py/manim_runner.py：模板系统 + AST 静态安全校验 + manim CLI 渲染
- skills/math-animation（默认模板路径）、skills/manim-codegen（自由代码路径）、skills/learning-animation

## 运行与预览
- 非预览型项目（库/插件，`project_type = ""`），`preview_enable = "disabled"`，无 `.preview`/`[dev]`
- 不可作为独立服务部署（无独立服务入口），`.coze` 无 `[deploy]`
- 构建：`pnpm run build`（tsc）
- 子进程需 python3 环境及 manim 依赖；worktree 交互如需跑渲染需先配置运行时

## 用户偏好与长期约束
- 无明确记录

## 常见问题和预防
- 插件为 peerDependency 模式，依赖 dsh 宿主环境加载，无法单独运行
- TS ⇄ Python 桥接通过子进程调用系统 python3，环境需安装 manim