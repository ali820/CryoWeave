import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-hydro-event'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./hydro_worker.py', import.meta.url))
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
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('hydro-event: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('hydro-event: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('hydro-event: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) throw new Error('hydro-event: allowedRoots must contain absolute paths')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Hydro operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('HYDRO_TIMEOUT', `Hydro operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('HYDRO_OUTPUT_TOO_LARGE', 'Hydro output exceeded 8 MiB.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('HYDRO_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Hydro operation was cancelled'))
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
        finish(resolve, failure('HYDRO_WORKER_FAILED', `Hydro worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'hydro_event',
    description: 'Compute DEM hypsometry, interpolate storage, assess water-balance closure, and run parameterized breach/weir scenarios. Local files require projectRoot inside allowedRoots. Hypsometry outputs are written only under stateRoot. Scenarios are explicit calculations, not forecasts.',
    parameters: {
      action: { type: 'string', required: true, enum: ['hypsometry', 'storage', 'water_balance', 'breach'], description: 'DEM curve, storage interpolation, closure assessment, or breach scenario.' },
      projectRoot: { type: 'string', required: true, description: 'Absolute project root inside allowedRoots.' },
      source: { type: 'string', description: 'DEM raster path for hypsometry, relative to projectRoot or absolute inside it.' },
      mask: { type: 'string', description: 'Optional lake vector mask for hypsometry.' },
      workingCrs: { type: 'string', description: 'Metric CRS for DEM area; defaults to EPSG:32645.' },
      elevationStepM: { type: 'number', description: 'Elevation bin step in meters; defaults to 1.' },
      minElevation: { type: 'number', description: 'Optional minimum elevation in meters.' },
      maxElevation: { type: 'number', description: 'Optional maximum elevation in meters.' },
      hypsometry: { type: 'string', description: 'Hypsometry CSV path with elev_m, area_below_km2, volume_km3; supports state://.' },
      levelM: { type: 'number', description: 'Water level for storage interpolation.' },
      components: { type: 'string', description: 'Water-balance component CSV with term, central_km3, lo_km3, hi_km3.' },
      outcomeKm3: { type: 'number', description: 'Observed or target change used for closure ratio and residual.' },
      outcomeTerm: { type: 'string', description: 'Optional outcome component excluded from input terms, for example 卓乃湖释放.' },
      nonSourceTerms: { type: 'array', items: { type: 'string' }, description: 'Non-source terms whose values are subtracted in closure accounting, for example 本地径流 and 湖面直接降水.' },
      preLevelM: { type: 'number', description: 'Initial water level for breach scenario.' },
      postLevelM: { type: 'number', description: 'Final water level for breach scenario.' },
      durationDays: { type: 'number', description: 'Breach duration in days; defaults to 1.' },
      widthM: { type: 'number', description: 'Optional weir width for peak scenario.' },
      dischargeCoefficient: { type: 'number', description: 'Optional weir coefficient C_d.' },
      headM: { type: 'number', description: 'Optional head above weir for peak scenario.' },
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
