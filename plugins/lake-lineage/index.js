import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-lake-lineage'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./lineage_worker.py', import.meta.url))
const MAX_OUTPUT_BYTES = 12 * 1024 * 1024
const MAX_ERROR_BYTES = 32 * 1024

function failure(code, message) {
  return { ok: false, error: { code, message } }
}

function resolveConfig(config) {
  const pythonPath = config.pythonPath?.trim() || 'python'
  const timeoutMs = config.timeoutMs ?? 240000
  const maxHashBytes = config.maxHashBytes ?? 268435456
  const stateRoot = config.stateRoot
  const allowedRoots = config.allowedRoots
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('lake-lineage: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('lake-lineage: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('lake-lineage: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) throw new Error('lake-lineage: allowedRoots must contain absolute paths')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Lake lineage operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('LINEAGE_TIMEOUT', `Lake lineage operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('LINEAGE_OUTPUT_TOO_LARGE', 'Lineage output exceeded 12 MiB. Reduce polygons or events.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('LINEAGE_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Lake lineage operation was cancelled'))
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
        finish(resolve, failure('LINEAGE_WORKER_FAILED', `Lake lineage worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'lake_lineage',
    description: 'Inspect annual lake vectors, build lineages using dominant mutual overlap with ambiguity checks, export cohort/event ledgers, and compute present-day transport co-location. Local files require projectRoot inside allowedRoots. Outputs are written only under stateRoot. Co-location is planimetric context, not damage or risk prediction.',
    parameters: {
      action: { type: 'string', required: true, enum: ['inspect', 'build', 'colocate'], description: 'Inspect annual vectors, build lineage, or compute transport co-location.' },
      projectRoot: { type: 'string', required: true, description: 'Absolute project root inside allowedRoots.' },
      yearFiles: { type: 'array', items: { type: 'object', additionalProperties: true }, description: 'Annual vector entries: path string or {year,path}; required for inspect/build.' },
      workingCrs: { type: 'string', description: 'Metric CRS for overlap, area and distance; defaults to EPSG:32645.' },
      dominantOverlap: { type: 'number', description: 'Minimum intersection ratio in both years for continuation; defaults to 0.95.' },
      minorOverlap: { type: 'number', description: 'Minimum ratio for minor-event ledger; defaults to 0.001.' },
      simplifyMeters: { type: 'number', description: 'Optional geometry simplification tolerance; defaults to 0.' },
      maxPolygonsPerYear: { type: 'integer', description: 'Maximum polygons loaded per year, from 1 to 20000; defaults to 10000.' },
      startYear: { type: 'integer', description: 'Optional cohort start-year filter.' },
      endYear: { type: 'integer', description: 'Optional cohort end-year filter.' },
      requireComplete: { type: 'boolean', description: 'Require every year in a lineage range to be observed; defaults to true.' },
      sourceIdColumn: { type: 'string', description: 'Optional source lake id column for sourceIds filtering.' },
      sourceIds: { type: 'array', items: { type: 'string' }, description: 'Optional source lake ids to include.' },
      cohort: { type: 'string', description: 'Cohort GeoJSON path for colocate, including state:// paths.' },
      transport: { type: 'string', description: 'Transport vector path for colocate, relative to projectRoot or absolute inside it.' },
      sourceCrs: { type: 'string', description: 'Fallback CRS for missing GeoJSON metadata; defaults to EPSG:4326.' },
      transportCrs: { type: 'string', description: 'Fallback transport CRS; defaults to sourceCrs.' },
      distanceThresholdsKm: { type: 'array', items: { type: 'number' }, description: 'Co-location distance thresholds; defaults to [1,3,5,10].' },
      limit: { type: 'integer', description: 'Maximum co-location rows, from 1 to 1000; defaults to 100.' },
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
