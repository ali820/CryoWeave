import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-lake-mapper'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./lake_worker.py', import.meta.url))
const MAX_OUTPUT_BYTES = 10 * 1024 * 1024
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
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('lake-mapper: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('lake-mapper: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('lake-mapper: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) throw new Error('lake-mapper: allowedRoots must contain absolute paths')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Lake operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('LAKE_TIMEOUT', `Lake operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('LAKE_OUTPUT_TOO_LARGE', 'Lake output exceeded 10 MiB. Reduce polygon limit.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('LAKE_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Lake operation was cancelled'))
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
        finish(resolve, failure('LAKE_WORKER_FAILED', `Lake worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'lake_mapper',
    description: 'Inspect lake rasters/vectors, extract water polygons from a mask or MNDWI/NDWI, and calculate morphology (area, perimeter, centroid, representative point, axis lengths, elongation, compactness, shape index, fractal dimension). Local files must be inside allowedRoots. Writes water_polygons.geojson, morphology.csv and manifest only under stateRoot.',
    parameters: {
      action: { type: 'string', required: true, enum: ['inspect', 'extract', 'morphology'], description: 'Raster/vector inspection, water extraction, or vector morphology.' },
      projectRoot: { type: 'string', description: 'Absolute project root inside allowedRoots; required for local sources.' },
      source: { type: 'string', description: 'Vector path for inspect/morphology, relative to projectRoot, absolute inside it, or state:// under stateRoot.' },
      inputs: { type: 'object', additionalProperties: true, description: 'Raster roles (mask,green,nir,swir1) to paths or {path,band}.' },
      method: { type: 'string', enum: ['mask', 'mndwi', 'ndwi'], description: 'Extraction method; defaults to mndwi.' },
      waterValues: { type: 'array', items: { type: 'number' }, description: 'Water class values for method=mask; defaults to [1].' },
      threshold: { type: 'number', description: 'Index threshold for mndwi/ndwi; defaults to 0.' },
      operator: { type: 'string', enum: ['gt', 'ge', 'lt', 'le'], description: 'Threshold comparison; defaults to gt.' },
      reflectanceScale: { type: 'number', description: 'Scale applied to green/nir/swir1; defaults to 1.' },
      reflectanceOffset: { type: 'number', description: 'Offset applied after reflectanceScale; defaults to 0.' },
      maskNodata: { type: 'number', description: 'Optional numeric value excluded from every raster.' },
      connectivity: { type: 'integer', description: 'Connected-component connectivity, 4 or 8; defaults to 8.' },
      sourceCrs: { type: 'string', description: 'Source CRS when vector metadata is missing, for example EPSG:4326.' },
      workingCrs: { type: 'string', description: 'Metric CRS for area, perimeter and axes; defaults to EPSG:32645.' },
      minAreaKm2: { type: 'number', description: 'Minimum polygon area in km²; defaults to 0.001 (1000 m²).' },
      simplifyMeters: { type: 'number', description: 'Optional geometry simplification tolerance in meters; defaults to 0.' },
      keepLargest: { type: 'boolean', description: 'Keep only the largest polygon; defaults to false.' },
      limit: { type: 'integer', description: 'Maximum polygons returned, from 1 to 1000; defaults to 100.' },
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
