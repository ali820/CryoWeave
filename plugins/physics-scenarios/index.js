import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-physics-scenarios'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./physics_worker.py', import.meta.url))
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
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('physics-scenarios: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('physics-scenarios: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('physics-scenarios: stateRoot must be absolute')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Physics operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('PHYSICS_TIMEOUT', `Physics operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('PHYSICS_OUTPUT_TOO_LARGE', 'Physics output exceeded 8 MiB.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('PHYSICS_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Physics operation was cancelled'))
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
        finish(resolve, failure('PHYSICS_WORKER_FAILED', `Physics worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'physics_scenarios',
    description: 'Run analytic thermal conduction and Terzaghi consolidation scenarios, parameter ensembles, and conservation/range checks without implying a full coupled solver. Outputs are written only under stateRoot. All results are scenario calculations, not forecasts.',
    parameters: {
      action: { type: 'string', required: true, enum: ['thermal', 'consolidation', 'ensemble'], description: 'Analytic thermal, consolidation, or thermal ensemble scenario.' },
      conductivityWmK: { type: 'number', description: 'Thermal conductivity W/(m·K); defaults to 2.' },
      densityKgM3: { type: 'number', description: 'Density kg/m³; defaults to 2400.' },
      heatCapacityJkgK: { type: 'number', description: 'Heat capacity J/(kg·K); defaults to 960.' },
      meanSurfaceC: { type: 'number', description: 'Mean surface temperature °C; defaults to 14.2.' },
      amplitudeC: { type: 'number', description: 'Surface temperature amplitude °C; defaults to 12.' },
      periodDays: { type: 'number', description: 'Thermal cycle period in days; defaults to 1.' },
      phaseRad: { type: 'number', description: 'Thermal phase offset in radians.' },
      depthsM: { type: 'array', items: { type: 'number' }, description: 'Depths for thermal output in meters.' },
      timesDays: { type: 'array', items: { type: 'number' }, description: 'Times for thermal/consolidation output in days.' },
      coefficientOfConsolidationM2PerYear: { type: 'number', description: 'Terzaghi coefficient of consolidation m²/year.' },
      drainagePathM: { type: 'number', description: 'Drainage path in meters.' },
      compressionIndex: { type: 'number', description: 'Compression index Cc.' },
      initialVoidRatio: { type: 'number', description: 'Initial void ratio e0.' },
      initialStressKPa: { type: 'number', description: 'Initial effective stress kPa.' },
      finalStressKPa: { type: 'number', description: 'Final effective stress kPa.' },
      layerThicknessM: { type: 'number', description: 'Consolidating layer thickness in meters.' },
      scenario: { type: 'string', description: 'Ensemble scenario; currently thermal.' },
      baseParameters: { type: 'object', additionalProperties: true, description: 'Fixed thermal parameters for ensemble.' },
      parameterRanges: { type: 'object', additionalProperties: true, description: 'Uniform sampling ranges as {parameter:[min,max]}.' },
      samples: { type: 'integer', description: 'Ensemble sample count, from 2 to 10000; defaults to 100.' },
      randomState: { type: 'integer', description: 'Random seed; defaults to 42.' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: config.timeoutMs,
    isConcurrencySafe: false,
    execute: async (args, exec) => JSON.stringify(await runWorker({
      ...args,
      stateRoot: config.stateRoot,
      maxHashBytes: config.maxHashBytes,
    }, config, exec.signal)),
  })))
}
