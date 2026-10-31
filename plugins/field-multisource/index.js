import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-field-multisource'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./field_worker.py', import.meta.url))
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024
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
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('field-multisource: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('field-multisource: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('field-multisource: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) throw new Error('field-multisource: allowedRoots must contain absolute paths')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Field operation was cancelled'))
      return
    }
    const child = spawn(config.pythonPath, [workerPath], {
      stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
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
    const timer = setTimeout(() => stopWith(failure('FIELD_TIMEOUT', `Field operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('FIELD_OUTPUT_TOO_LARGE', 'Field output exceeded 8 MiB.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('FIELD_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Field operation was cancelled'))
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
        finish(resolve, failure('FIELD_WORKER_FAILED', `Field worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'field_multisource',
    description: 'Inspect CSV/Excel field observations, align sources in time and space, run observation QA, and compare paired values with explicit error metrics. Local files require projectRoot inside allowedRoots. Outputs are written only under stateRoot. Cross-site comparisons are not automatically sensor-accuracy validation.',
    parameters: {
      action: { type: 'string', required: true, enum: ['inspect', 'qc', 'align', 'compare'], description: 'Source inspection, quality control, source alignment, or paired comparison.' },
      projectRoot: { type: 'string', required: true, description: 'Absolute project root inside allowedRoots.' },
      source: { type: 'string', description: 'CSV/Excel source for inspect/qc.' },
      sheetName: { type: 'string', description: 'Excel worksheet name for inspect/qc/alignment fallback.' },
      headerRow: { type: 'integer', description: 'Zero-based Excel/CSV header row; defaults to 0.' },
      timeColumn: { type: 'string', description: 'Optional time column for inspect/qc.' },
      valueColumns: { type: 'array', items: { type: 'string' }, description: 'Numeric columns to inspect or check.' },
      minValue: { type: 'number', description: 'Optional lower bound for QC.' },
      maxValue: { type: 'number', description: 'Optional upper bound for QC.' },
      requiredColumns: { type: 'array', items: { type: 'string' }, description: 'Columns required by QC.' },
      duplicateKeyColumns: { type: 'array', items: { type: 'string' }, description: 'Columns used to detect duplicate observations.' },
      maxGapHours: { type: 'number', description: 'Maximum acceptable time gap for QC; defaults to 48.' },
      sourceA: { type: 'string', description: 'First source for alignment.' },
      sourceB: { type: 'string', description: 'Second source for alignment.' },
      sourceASheet: { type: 'string', description: 'Worksheet for sourceA.' },
      sourceBSheet: { type: 'string', description: 'Worksheet for sourceB.' },
      timeColumnA: { type: 'string', description: 'Time column for sourceA.' },
      timeColumnB: { type: 'string', description: 'Time column for sourceB.' },
      latColumnA: { type: 'string', description: 'Latitude column for sourceA.' },
      lonColumnA: { type: 'string', description: 'Longitude column for sourceA.' },
      latColumnB: { type: 'string', description: 'Latitude column for sourceB.' },
      lonColumnB: { type: 'string', description: 'Longitude column for sourceB.' },
      valueColumnA: { type: 'string', description: 'Value column to retain from sourceA.' },
      valueColumnB: { type: 'string', description: 'Value column to retain from sourceB.' },
      maxTimeDiffSeconds: { type: 'number', description: 'Maximum time difference for alignment; defaults to 86400.' },
      maxDistanceMeters: { type: 'number', description: 'Maximum spatial distance for alignment; defaults to 1000.' },
      maxPairs: { type: 'integer', description: 'Maximum aligned pairs; defaults to 10000.' },
      table: { type: 'string', description: 'Paired CSV or table for compare, including state:// paths.' },
      columnA: { type: 'string', description: 'First value column for compare.' },
      columnB: { type: 'string', description: 'Second value column for compare.' },
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
