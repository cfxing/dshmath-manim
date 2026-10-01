/**
 * skill.ts — 数学动画技能（Skill）注册。
 *
 * 面向“只懂数学物理、不需要了解实现细节”的用户。
 * 插件在加载时向 ctx.skills 注册 skills/ 目录下的全部技能。
 *
 * 产品层技能：
 *   - learning-animation  用户可直接调用的“学习动画”
 *
 * 内部实现技能：
 *   - math-animation      模板路径
 *   - manim-codegen       自由代码路径
 *
 * 用户只需要看到产品能力，模型仍可在内部加载实现技能。
 */

import { readdirSync, readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import type { Context } from '@deepseek-ai/cordis'
import type { SkillRegistration } from '@deepseek-ai/dsh-skill'

const __dirname = dirname(fileURLToPath(import.meta.url))
/** 编译后 dist/skill.js → 项目根上一级；技能位于项目根 skills/ 下。 */
export const SKILLS_DIR = join(__dirname, '..', 'skills')

export interface MathAnimationSkill {
  name: string
  description: string
  whenToUse?: string
  content: string
  modelInvocable?: boolean
  userInvocable?: boolean
}

/** 解析单个 SKILL.md：剥离 frontmatter，返回可注册的技能定义。 */
function parseSkillFile(file: string, fallbackName: string): MathAnimationSkill {
  const raw = readFileSync(file, 'utf8')
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(raw)
  if (!match) {
    return { name: fallbackName, description: `Skill ${fallbackName}`, content: raw }
  }
  const meta: Record<string, string> = {}
  for (const line of match[1].split('\n')) {
    const idx = line.indexOf(':')
    if (idx > 0) {
      const key = line.slice(0, idx).trim()
      const value = line.slice(idx + 1).trim().replace(/^["']|["']$/g, '')
      meta[key] = value
    }
  }

  const booleanMeta = (key: string, fallback = true): boolean => {
    const value = meta[key]
    if (value === undefined) return fallback
    return value === 'true'
  }

  return {
    name: meta.name ?? fallbackName,
    description: meta.description ?? `Skill ${fallbackName}`,
    whenToUse: meta.whenToUse || undefined,
    content: match[2].trim(),
    modelInvocable: meta['disable-model-invocation'] === 'true' ? false : booleanMeta('model-invocable', true),
    userInvocable: booleanMeta('user-invocable', true),
  }
}

/**
 * 扫描 skills/ 目录并解析全部技能。
 * 支持两种形态（与 dsh-skill-filesystem 一致）：
 *   - 目录 bundle：<root>/<name>/SKILL.md
 *   - 扁平文件：<root>/<name>.md
 */
export function listSkills(): MathAnimationSkill[] {
  const out: MathAnimationSkill[] = []
  if (!existsSync(SKILLS_DIR)) return out
  for (const entry of readdirSync(SKILLS_DIR, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      const file = join(SKILLS_DIR, entry.name, 'SKILL.md')
      if (existsSync(file)) out.push(parseSkillFile(file, entry.name))
    } else if (entry.isFile() && entry.name.endsWith('.md')) {
      out.push(parseSkillFile(join(SKILLS_DIR, entry.name), entry.name.replace(/\.md$/, '')))
    }
  }
  return out
}

/**
 * 在运行时注册全部数学动画技能。
 * 由每个 SKILL.md 的 frontmatter 决定模型/用户可见性。
 *
 * 未写调用策略的旧技能保持双可调用，避免破坏现有部署；
 * 新的内部实现技能显式使用 user-invocable: false。
 */
export function registerSkills(ctx: Context) {
  for (const skill of listSkills()) {
    const registration: SkillRegistration = {
      ...skill,
      source: 'runtime',
      provider: 'math-manim',
      invocation: {
        modelInvocable: skill.modelInvocable ?? true,
        userInvocable: skill.userInvocable ?? true,
      },
    }
    ctx.skills.register(registration)
  }
}
