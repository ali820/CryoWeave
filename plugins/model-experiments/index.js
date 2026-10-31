import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-model-experiments'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./model_worker.py', import.meta.url))
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024
const MAX_ERROR_BYTES = 32 * 1024

function failure(code, message) {
  return { ok: false, error: { code, message } }
}

function resolveConfig(config) {
  const pythonPath = config.pythonPath?.trim() || 'python'
  const timeoutMs = config.timeoutMs ?? 180000
  const maxHashBytes = config.maxHashBytes ?? 268435456
  const stateRoot = config.stateRoot
  const allowedRoots = config.allowedRoots
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('model-experiments: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('model-experiments: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('model-experiments: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) throw new Error('model-experiments: allowedRoots must contain absolute paths')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Model operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('MODEL_TIMEOUT', `Model operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('MODEL_OUTPUT_TOO_LARGE', 'Model output exceeded 8 MiB.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('MODEL_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Model operation was cancelled'))
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
        finish(resolve, failure('MODEL_WORKER_FAILED', `Model worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'model_experiments',
    description: 'Run grouped and rolling model validation, baseline comparisons, permutation importance, and explicit leakage checks on research tables. Local files require projectRoot inside allowedRoots. Outputs are written only under stateRoot. Importance is not SHAP and does not establish causality.',
    parameters: {
      action: { type: 'string', required: true, enum: ['evaluate', 'backtest', 'importance'], description: 'Cross-validation, rolling backtest, or permutation importance.' },
      projectRoot: { type: 'string', required: true, description: 'Absolute project root inside allowedRoots.' },
      table: { type: 'string', description: 'CSV feature/target table, relative to projectRoot or absolute inside it.' },
      featureColumns: { type: 'array', required: true, items: { type: 'string' }, description: 'Numeric feature columns.' },
      targetColumn: { type: 'string', required: true, description: 'Target column; must not be included in featureColumns.' },
      task: { type: 'string', enum: ['classification', 'regression'], description: 'Task type; defaults to classification.' },
      positiveLabel: { type: 'string', description: 'Positive class label for classification.' },
      groupColumn: { type: 'string', description: 'Group column for GroupKFold spatial/blocked validation.' },
      model: { type: 'string', description: 'random_forest, logistic, ridge, or linear depending on task; defaults to random_forest.' },
      cvFolds: { type: 'integer', description: 'Cross-validation folds, from 2 to 20; defaults to 5.' },
      nEstimators: { type: 'integer', description: 'Random forest trees, from 1 to 1000; defaults to 100.' },
      maxDepth: { type: 'integer', description: 'Optional random forest maximum depth.' },
      randomState: { type: 'integer', description: 'Random seed; defaults to 42.' },
      nRepeats: { type: 'integer', description: 'Permutation importance repeats; defaults to 5.' },
      orderColumn: { type: 'string', description: 'Ordering column for rolling backtest.' },
      initialTrainFraction: { type: 'number', description: 'Initial training fraction for backtest; defaults to 0.6.' },
      testFraction: { type: 'number', description: 'Test block fraction for backtest; defaults to 0.2.' },
      stepFraction: { type: 'number', description: 'Training expansion step for backtest; defaults to 0.2.' },
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
