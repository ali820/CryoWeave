import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-research-run'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./run_worker.py', import.meta.url))
const MAX_OUTPUT_BYTES = 6 * 1024 * 1024
const MAX_ERROR_BYTES = 32 * 1024

function failure(code, message) {
  return { ok: false, error: { code, message } }
}

function resolveConfig(config) {
  const pythonPath = config.pythonPath?.trim() || 'python'
  const timeoutMs = config.timeoutMs ?? 120000
  const maxHashBytes = config.maxHashBytes ?? 268435456
  const stateRoot = config.stateRoot
  const allowedRoots = config.allowedRoots
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) {
    throw new Error('research-run: timeoutMs must be an integer from 1000 to 600000')
  }
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) {
    throw new Error('research-run: maxHashBytes must be an integer from 1024 to 2147483648')
  }
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) {
    throw new Error('research-run: stateRoot must be an absolute path')
  }
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) {
    throw new Error('research-run: allowedRoots must be a non-empty array of absolute paths')
  }
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Research run operation was cancelled'))
      return
    }
    const child = spawn(config.pythonPath, [workerPath], {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONNOUSERSITE: '1' },
    })
    let stdout = ''
    let stderr = ''
    let settled = false
    let terminalFailure
    let aborted = false

    const cleanup = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
    }
    const finish = (callback, value) => {
      if (settled) return
      settled = true
      cleanup()
      callback(value)
    }
    const stopWith = (result) => {
      terminalFailure = result
      child.kill()
    }
    const onAbort = () => {
      aborted = true
      child.kill()
    }
    const timer = setTimeout(() => {
      stopWith(failure('RUN_TIMEOUT', `Research run operation exceeded ${config.timeoutMs} ms.`))
    }, config.timeoutMs)

    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) {
        stopWith(failure('RUN_OUTPUT_TOO_LARGE', 'Run worker output exceeded 6 MiB. Use list filters or a smaller limit.'))
      }
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => {
      finish(resolve, failure('RUN_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`))
    })
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Research run operation was cancelled'))
        return
      }
      if (terminalFailure) {
        finish(resolve, terminalFailure)
        return
      }
      try {
        finish(resolve, JSON.parse(stdout.trim()))
      } catch {
        const diagnostic = stderr.trim().slice(-8000)
        finish(resolve, failure('RUN_WORKER_FAILED', `Run worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'research_run',
    description: 'Manage a research run ledger without executing project scripts. create: projectRoot+pipeline with optional config/inputs/steps; list: optional pipeline/status/projectRoot/limit; get/verify: runId; update_status: runId+status with optional note/step/stepStatus; register_artifacts: runId+artifacts with optional status/step. Writes manifests only to the configured stateRoot. Source projects are read-only. Sensitive config keys are redacted. verify detects missing or changed inputs/outputs.',
    parameters: {
      action: { type: 'string', required: true, enum: ['create', 'list', 'get', 'update_status', 'register_artifacts', 'verify'], description: 'Run ledger operation.' },
      runId: { type: 'string', description: 'Exact run id returned by create; required for get, update_status, register_artifacts, and verify.' },
      projectRoot: { type: 'string', description: 'Absolute project root inside allowedRoots; required for create and optional as a list filter.' },
      pipeline: { type: 'string', description: 'Pipeline id using letters, numbers, dots, underscores, or hyphens; required for create and optional as a list filter.' },
      config: { type: 'object', additionalProperties: true, description: 'Optional JSON run configuration. Sensitive key names are redacted before storage.' },
      inputs: { type: 'array', items: { type: 'string' }, description: 'Input file paths relative to projectRoot; hashed at creation.' },
      steps: { type: 'array', items: { type: 'string' }, description: 'Optional ordered step names; defaults to [run].' },
      artifacts: { type: 'array', items: { type: 'string' }, description: 'Output files relative to projectRoot, or state:// paths under stateRoot; required for register_artifacts.' },
      status: { type: 'string', enum: ['planned', 'running', 'complete', 'failed', 'cancelled'], description: 'New run status or list filter.' },
      step: { type: 'string', description: 'Optional existing step name to update.' },
      stepStatus: { type: 'string', enum: ['planned', 'running', 'complete', 'failed', 'cancelled'], description: 'Optional step status; defaults to status, or complete when registering artifacts to a step.' },
      note: { type: 'string', description: 'Optional lifecycle note, at most 4000 characters.' },
      limit: { type: 'integer', description: 'Maximum runs returned by list, from 1 to 200; defaults to 50.' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: config.timeoutMs,
    isConcurrencySafe: false,
    execute: async (args, exec) => JSON.stringify(await runWorker({
      ...args,
      allowedRoots: config.allowedRoots,
      stateRoot: config.stateRoot,
      maxHashBytes: config.maxHashBytes,
    }, config, exec.signal)),
  })))
}
