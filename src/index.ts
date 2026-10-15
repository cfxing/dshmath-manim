/**
 * index.ts — dshmath-manim 插件入口。
 *
 * 注册一组教学动画工具（基于 Manim CE），模型可通过自然语言生成各科动画。
 *
 * 用法（在 dsh 配置中加载）：
 *   - insert:
 *       - id: dshmath-manim
 *         name: 'dshmath-manim'
 */

import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { listTemplates, renderScene, renderCode, validateScene } from './runner.js'
import { registerSkills } from './skill.js'

export const name = 'dshmath-manim'
export const description = 'Manim CE 学习动画插件：将数学、物理、生物、化学等知识渲染为教学视频'

export const inject = ['tools', 'skills']

/** 插件配置：outdir 对应 cordis.yml 中的 config.outdir，不配置时用插件 out/ 目录 */
export interface Config {
  outdir?: string
  ttsModel?: string
  ttsVoice?: string
  ttsSpeed?: number
  narrationSegments?: string[]
}

/** 同名 Schema：Cordis 加载插件时据此校验配置并填充默认值 */
export const Config: Schema<Config> = Schema.object({
  outdir: Schema.string(),
  ttsModel: Schema.string(),
  ttsVoice: Schema.string().default("zh-CN-XiaoxiaoNeural"),
  ttsSpeed: Schema.number().default(0.97),
})

export function apply(ctx: Context, config: Config) {
  // ---------------------------------------------------------------------
  // 技能包 — 零代码提示词引导（面向不懂代码的用户）
  //   math-animation：模板路径（默认推荐）
  //   manim-codegen：自由代码路径（模型直接写 Manim 场景）
  // ---------------------------------------------------------------------
  registerSkills(ctx)

  // ---------------------------------------------------------------------
  // list_templates — 列出可用教学模板及参数模式
  // ---------------------------------------------------------------------
  ctx.tools.register(
    defineTool({
      name: 'list_math_templates',
      description:
        'List available educational animation templates, including math, biology cells, chemical reactions, and general step-by-step explanations. Use this first to find a suitable scene.',
      parameters: {},
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value: any) => {
          const templates = value?.templates ?? {}
          const lines = Object.entries(templates).map(([k, v]: any) => {
            const required = Object.entries(v?.parameters ?? {})
              .filter(([, s]: any) => s.required)
              .map(([pk]) => pk)
            return `- ${k}: ${v?.description ?? ''}${required.length ? ` (required: ${required.join(', ')})` : ''}`
          })
          return [{ type: 'text', text: `Available animation templates:\n${lines.join('\n') || '(none)'}` }]
        },
      },
      async execute(_args, exec) {
        const res = await listTemplates(exec.signal)
        if (!res.ok || !res.data) {
          throw new Error(`Failed to list templates: ${res.stderrTail ?? 'unknown error'}`)
        }
        return res.data
      },
    }),
  )

  // ---------------------------------------------------------------------
  // render_math_scene — 用模板渲染教学动画（推荐，安全）
  // ---------------------------------------------------------------------
  ctx.tools.register(
    defineTool({
      name: 'render_math_scene',
      description:
        'Render an educational animation video from a template. Supports math, biology cell structure, chemical reactions, and general learning processes. Pick a template with list_math_templates.',
      parameters: {
        template: { type: 'string', required: true, description: 'Template name, e.g. "function_plot"' },
        params: {
          type: 'object',
          additionalProperties: true,
          description: 'Template parameters as a JSON object; see list_math_templates for the selected template schema.',
        },
        quality: {
          type: 'string',
          enum: ['low', 'medium', 'high', 'ultra'],
          description: 'Render quality. Use "low" for quick preview, "high" for final output.',
        },
        narration_segments: {
          type: 'array',
          items: { type: 'string' },
          description: 'Optional segmented Chinese narration. One sentence per animation step; the plugin measures each sentence and builds the shared RUN timeline.',
        },
        narration: {
          type: 'string',
          description: 'Optional Chinese narration. When provided, edge-tts audio using Xiaoxiao is generated and muxed into the MP4.',
        },
        tts_model: { type: 'string', description: 'Deprecated and ignored; edge-tts does not use a local model.' },
        tts_voice: { type: 'string', description: 'Optional edge-tts voice; default zh-CN-XiaoxiaoNeural (晓晓).' },
        tts_speed: { type: 'number', description: 'Playback speed applied with FFmpeg atempo. Default 0.97. Range 0.5-2.0.' },
        outdir: { type: 'string', description: 'Output directory. Defaults to plugin out/.' },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value: any) => {
          const base = value?.ok
            ? `Rendered ${value.template} to ${value.video} (${(value.size_bytes ?? 0) / 1024} KB, quality=${value.quality})`
            : `Render failed: ${value?.error ?? 'unknown error'}`
          return [{ type: 'text', text: value?.timing_warning ? `${base}\n⚠️ ${value.timing_warning}` : base }]
        },
      },
      async execute(args, exec) {
        const res = await renderScene(
          {
            template: args.template,
            params: (args.params ?? {}) as Record<string, unknown>,
            quality: args.quality ?? 'low',
            outdir: args.outdir ?? config.outdir,
            narration: args.narration_segments?.length ? JSON.stringify(args.narration_segments) : args.narration,
            ttsModel: args.tts_model ?? config.ttsModel,
            ttsVoice: args.tts_voice ?? config.ttsVoice,
            ttsSpeed: args.tts_speed ?? config.ttsSpeed,
          },
          exec.signal,
        )
        if (!res.data) {
          throw new Error(`render_math_scene: ${res.stderrTail ?? 'no output'}`)
        }
        return res.data
      },
    }),
  )

  // ---------------------------------------------------------------------
  // render_math_code — 直接渲染模型自写的 Manim 场景（进阶，经安全校验）
  // ---------------------------------------------------------------------
  ctx.tools.register(
    defineTool({
      name: 'render_math_code',
      description:
        'Render a custom Manim CE educational scene from model-written Python source. Use for biology, chemistry or other subjects when no template fits; code is statically checked.',
      parameters: {
        code: { type: 'string', required: true, description: 'Complete Manim Python scene source code (class extending Scene).' },
        quality: { type: 'string', enum: ['low', 'medium', 'high', 'ultra'], description: 'Render quality. Default low.' },
        outdir: { type: 'string', description: 'Output directory. Defaults to plugin out/.' },
        narration_segments: { type: 'array', items: { type: 'string' }, description: 'Optional segmented Chinese narration; one sentence per animation step.' },
        narration: { type: 'string', description: 'Optional narration text to synthesize with edge-tts and mux into the MP4.' },
        tts_model: { type: 'string', description: 'Deprecated and ignored; edge-tts does not use a local model.' },
        tts_voice: { type: 'string', description: 'Optional edge-tts voice; default zh-CN-XiaoxiaoNeural (晓晓).' },
        tts_speed: { type: 'number', description: 'Playback speed applied with FFmpeg atempo. Default 0.97. Range 0.5-2.0.' },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value: any) => {
          const base = value?.ok
            ? `Rendered custom scene to ${value.video}`
            : `Render failed: ${value?.error ?? 'unknown error'}`
          return [{ type: 'text', text: value?.timing_warning ? `${base}\n⚠️ ${value.timing_warning}` : base }]
        },
      },
      async execute(args, exec) {
        const res = await renderCode({
          code: args.code,
          quality: args.quality ?? 'low',
          outdir: args.outdir ?? config.outdir,
          narration: args.narration_segments?.length ? JSON.stringify(args.narration_segments) : args.narration,
          ttsModel: args.tts_model ?? config.ttsModel,
          ttsVoice: args.tts_voice ?? config.ttsVoice,
          ttsSpeed: args.tts_speed ?? config.ttsSpeed,
        }, exec.signal)
        if (!res.data) {
          throw new Error(`render_math_code: ${res.stderrTail ?? 'no output'}`)
        }
        return res.data
      },
    }),
  )

  // ---------------------------------------------------------------------
  // validate_math_code — 静态安全校验（供自愈循环使用）
  // ---------------------------------------------------------------------
  ctx.tools.register(
    defineTool({
      name: 'validate_math_code',
      description:
        'Static safety-check Manim Python code without rendering. Returns a list of violations. Use before render_math_code to catch dangerous imports or calls.',
      parameters: {
        code: { type: 'string', required: true, description: 'Manim scene source code to validate.' },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value: any) => [
          {
            type: 'text',
            text: value?.ok
              ? 'Code passed static validation.'
              : `Code rejected: ${(value?.violations ?? []).join('; ')}`,
          },
        ],
      },
      async execute(args, exec) {
        const res = await validateScene(args.code, exec.signal)
        if (!res.data) {
          throw new Error(`validate_math_code: ${res.stderrTail ?? 'no output'}`)
        }
        return res.data
      },
    }),
  )
}
