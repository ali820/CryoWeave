import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-environmental-indices'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./indices_worker.py', import.meta.url))
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
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('environmental-indices: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('environmental-indices: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('environmental-indices: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) throw new Error('environmental-indices: allowedRoots must contain absolute paths')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Environmental index operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('INDICES_TIMEOUT', `Environmental index operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('INDICES_OUTPUT_TOO_LARGE', 'Index output exceeded 8 MiB. Reduce indices or TVDI bins.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('INDICES_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Environmental index operation was cancelled'))
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
        finish(resolve, failure('INDICES_WORKER_FAILED', `Index worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'environmental_indices',
    description: 'Inspect aligned local rasters or calculate NDVI, FVC, LST, NDMI, NDSI, and TVDI. inputs maps roles (red,nir,green,swir1,ndvi,lst,qa) to raster paths or {path,band}. calculate writes indices.tif, stats.csv, TVDI fit and manifest only under stateRoot. Aligns different grids to the first raster; source projects are read-only.',
    parameters: {
      action: { type: 'string', required: true, enum: ['inspect', 'calculate'], description: 'Raster inspection or index calculation.' },
      projectRoot: { type: 'string', required: true, description: 'Absolute project root inside allowedRoots.' },
      inputs: { type: 'object', additionalProperties: true, description: 'Role to raster path or {path,band}. Roles: red,nir,green,swir1,ndvi,lst,qa. Paths are relative to projectRoot or absolute inside it.' },
      indices: { type: 'array', items: { type: 'string', enum: ['ndvi', 'fvc', 'lst', 'ndmi', 'ndsi', 'tvdi'] }, description: 'Indices to calculate. Defaults to all; required input roles depend on the selection.' },
      reflectanceScale: { type: 'number', description: 'Scale applied to red/nir/green/swir1 before index calculation; defaults to 1.' },
      reflectanceOffset: { type: 'number', description: 'Offset applied after reflectanceScale; defaults to 0.' },
      ndviScale: { type: 'number', description: 'Scale applied to an existing NDVI raster; defaults to 1.' },
      ndviOffset: { type: 'number', description: 'Offset applied after ndviScale; defaults to 0.' },
      lstScale: { type: 'number', description: 'Scale applied to LST before unit conversion; defaults to 1.' },
      lstOffset: { type: 'number', description: 'Offset applied after lstScale; defaults to 0.' },
      lstUnit: { type: 'string', enum: ['C', 'K'], description: 'LST input unit after scale/offset; defaults to Celsius.' },
      reflectanceMin: { type: 'number', description: 'Minimum valid reflectance; defaults to -1.' },
      reflectanceMax: { type: 'number', description: 'Maximum valid reflectance; defaults to 2.' },
      ndviValidMin: { type: 'number', description: 'Minimum valid NDVI; defaults to -1.' },
      ndviValidMax: { type: 'number', description: 'Maximum valid NDVI; defaults to 1.' },
      lstMinC: { type: 'number', description: 'Minimum valid LST in Celsius; defaults to -100.' },
      lstMaxC: { type: 'number', description: 'Maximum valid LST in Celsius; defaults to 100.' },
      maskNodata: { type: 'number', description: 'Optional numeric value to exclude from every input raster.' },
      qaMode: { type: 'string', enum: ['landsat'], description: 'Apply Landsat QA_PIXEL fill/dilated/cirrus/cloud/cloud-shadow masks and optionally snow.' },
      keepSnow: { type: 'boolean', description: 'Keep snow pixels when qaMode=landsat; defaults to false.' },
      fvcNdviMin: { type: 'number', description: 'NDVI for bare-soil FVC=0; defaults to 0.05.' },
      fvcNdviMax: { type: 'number', description: 'NDVI for full-cover FVC=1; defaults to 0.8.' },
      ndviBins: { type: 'integer', description: 'TVDI NDVI bins from 2 to 200; defaults to 20.' },
      minBinPoints: { type: 'integer', description: 'Minimum points per populated TVDI bin; defaults to 30.' },
      dryPercentile: { type: 'number', description: 'Dry-edge LST percentile; defaults to 95.' },
      wetPercentile: { type: 'number', description: 'Wet-edge LST percentile; defaults to 5.' },
      tvdiNdviMin: { type: 'number', description: 'Minimum NDVI included in TVDI feature space; defaults to 0.' },
      tvdiNdviMax: { type: 'number', description: 'Maximum NDVI included in TVDI feature space; defaults to 1.' },
      maxPixels: { type: 'integer', description: 'Maximum raster pixels from 1000 to 200000000; defaults to 20000000.' },
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
