/**
 * dsh-loop-guard —— 挂到 DSH 工具管线上的运行时护栏（Cordis 插件，零依赖）。
 *
 * 挂在三个事件上，都是"执行路径"而不是提示词：
 *
 *   tools/pre-execute   危险命令硬拒绝 / 同参数重复调用熔断 / 单轮工具调用预算
 *   tools/post-execute  连续失败达阈值 → block，把"停止重试"变成模型可见的错误结果
 *   agent/pre-step      用户新消息进来时重置本轮计数（否则预算会跨轮累积）
 *
 * 设计原则：任何内部异常都 fall through 到 next()。护栏出错宁可放行，
 * 也不能把整条工具管线弄挂。
 *
 * 加载方式见同目录 README；配置项与 governance/policy.yaml 对应。
 */

export const name = 'loop-guard'

/** 需要 tools 服务；监听器随插件 dispose 一起卸载。 */
export const inject = ['tools']

export const DEFAULTS = {
  maxToolCalls: 120,
  maxConsecutiveFailures: 2,
  duplicateCallThreshold: 3,
}

/**
 * 与 governance/policy.yaml 的 safety.deny_command_patterns 保持一致。
 * 这类规则极少变动，内联避免让插件依赖 YAML 解析器；改动时两处同步，audit 会提示。
 */
const DENY_PATTERNS = [
  [/\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+\/(?:\s|$)/, 'rm -rf /'],
  [/\bgit\s+push\b[^\n]*--force/, 'git push --force'],
  [/\bgit\s+reset\s+--hard\b/, 'git reset --hard'],
  [/\b(?:drop|truncate)\s+(?:table|database)\b/i, 'DROP/TRUNCATE'],
  [/\bchmod\s+777\b/, 'chmod 777'],
  [/\bcurl\b[^\n|]*\|\s*(?:ba|z|k)?sh\b/, 'curl | sh'],
  [/\bdd\s+if=[^\n]*of=\/dev\//, 'dd to /dev'],
  [/\bmkfs\b/, 'mkfs'],
  [/\bsudo\b/, 'sudo'],
]

/** 参数的规范化形式：深度排序后 stringify，避免键序不同被当成新调用。 */
export function canonicalize(value) {
  return JSON.stringify(sortValue(value))
}

function sortValue(value) {
  if (Array.isArray(value)) return value.map(sortValue)
  if (value !== null && typeof value === 'object') {
    const sorted = {}
    for (const key of Object.keys(value).sort()) sorted[key] = sortValue(value[key])
    return sorted
  }
  return value
}

export function fingerprint(tool, args) {
  return `${tool}::${canonicalize(args)}`
}

/** 从任意工具的参数字段里找命令文本（bash 的 command，或自定义工具的 cmd）。 */
function commandText(exec) {
  const args = exec.arguments
  if (typeof args === 'string') return args
  if (!args || typeof args !== 'object') return ''
  for (const key of ['command', 'cmd', 'script', 'shell']) {
    const value = args[key]
    if (typeof value === 'string') return value
  }
  return ''
}

export function matchDangerous(exec) {
  const text = commandText(exec)
  if (!text) return null
  for (const [pattern, label] of DENY_PATTERNS) {
    if (pattern.test(text)) return label
  }
  return null
}

const deny = (reason, code) => ({
  kind: 'deny',
  reason,
  info: { name: 'LoopGuardDenied', code },
})

/**
 * 安装护栏。
 * @param ctx Cordis 插件上下文
 * @param config 来自 patch 的 config（没有 Config schema 时原样传入）
 */
export function apply(ctx, config = {}) {
  const cfg = { ...DEFAULTS, ...(config ?? {}) }
  const states = new WeakMap()
  let anonymousState = null

  const stateFor = (agent) => {
    if (!agent) return (anonymousState ??= makeState())
    let state = states.get(agent)
    if (!state) states.set(agent, (state = makeState()))
    return state
  }

  const reset = (state) => {
    state.calls = 0
    state.failures = 0
    state.nudged = false
    state.deniedCallId = null
    state.seen.clear()
  }

  ctx.logger?.info?.(
    `[loop-guard] loaded: maxToolCalls=${cfg.maxToolCalls} ` +
      `maxConsecutiveFailures=${cfg.maxConsecutiveFailures} duplicateThreshold=${cfg.duplicateCallThreshold}`,
  )

  // 用户发新消息 = 新任务边界：重置上一轮的预算与失败计数
  ctx.on('agent/pre-step', ({ agent, messages }, next) => {
    if (Array.isArray(messages) && messages.some((message) => message?.source?.kind === 'user')) {
      reset(stateFor(agent))
    }
    return next()
  })

  ctx.on('tools/pre-execute', async (exec, next) => {
    try {
      const dangerous = matchDangerous(exec)
      if (dangerous) {
        return deny(
          `安全策略拒绝执行：命中危险模式「${dangerous}」。该操作不可逆，不允许在 agent 循环内执行；如确需执行，请让用户手动完成。`,
          'LOOP_GUARD_DANGEROUS',
        )
      }

      const state = stateFor(exec.agent)
      const key = fingerprint(exec.name, exec.arguments)
      const count = (state.seen.get(key) ?? 0) + 1
      state.seen.set(key, count)

      if (count >= cfg.duplicateCallThreshold) {
        state.deniedCallId = exec.callId
        return deny(
          `同一调用已重复 ${count} 次（工具 ${exec.name}，参数完全相同）。停止重复：改用不同参数，` +
            `或直接使用已有结果；若确实无法推进，请向用户说明卡点，不要继续重试。`,
          'LOOP_GUARD_DUPLICATE',
        )
      }

      state.calls += 1
      if (state.calls > cfg.maxToolCalls) {
        state.deniedCallId = exec.callId
        return deny(
          `本轮工具调用已达上限 ${cfg.maxToolCalls} 次。停止执行，先汇报已完成内容、` +
            `未完成项与续跑点，由用户决定是否继续。`,
          'LOOP_GUARD_BUDGET',
        )
      }

      return next()
    } catch (error) {
      ctx.logger?.warn?.(`[loop-guard] pre-execute 异常，已放行: ${error?.stack ?? error}`)
      return next()
    }
  })

  ctx.on('tools/post-execute', async (exec, result, next) => {
    try {
      const state = stateFor(exec.agent)
      if (result?.isError) {
        // 被本插件拒绝的调用不计入失败连击，避免"拒绝一次又触发纠正"的双重记账
        if (state.deniedCallId === exec.callId) state.deniedCallId = null
        else state.failures += 1
      } else {
        state.failures = 0
        state.nudged = false
      }

      const downstream = await next()
      if (state.failures >= cfg.maxConsecutiveFailures && !state.nudged) {
        state.nudged = true
        if (downstream.kind === 'block') return downstream
        const detail = result?.error?.message ? `最后错误：${result.error.message}` : ''
        const feedback = [
          {
            type: 'text',
            text:
              `[loop-guard] 同一工具已连续失败 ${state.failures} 次，重试已停止。${detail}\n` +
              `请说明失败原因、已尝试的参数，以及下一步需要用户提供什么；不要再用相同参数重试。`,
          },
        ]
        return { kind: 'block', feedback, additionalContexts: downstream.additionalContexts }
      }
      return downstream
    } catch (error) {
      ctx.logger?.warn?.(`[loop-guard] post-execute 异常，已放行: ${error?.stack ?? error}`)
      return next()
    }
  })
}

function makeState() {
  return {
    calls: 0,
    failures: 0,
    nudged: false,
    deniedCallId: null,
    seen: new Map(),
  }
}
