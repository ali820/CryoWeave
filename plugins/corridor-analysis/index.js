import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-corridor-analysis'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./corridor_worker.py', import.meta.url))
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
  const geeProject = config.geeProject?.trim() || undefined
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) {
    throw new Error('corridor-analysis: timeoutMs must be an integer from 1000 to 600000')
  }
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) {
    throw new Error('corridor-analysis: maxHashBytes must be an integer from 1024 to 2147483648')
  }
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('corridor-analysis: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) {
    throw new Error('corridor-analysis: allowedRoots must be a non-empty array of absolute paths')
  }
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots, geeProject }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Corridor operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('CORRIDOR_TIMEOUT', `Corridor operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('CORRIDOR_OUTPUT_TOO_LARGE', 'Corridor output exceeded 10 MiB. Reduce the group limit.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('CORRIDOR_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Corridor operation was cancelled'))
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
        finish(resolve, failure('CORRIDOR_WORKER_FAILED', `Corridor worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'corridor_analysis',
    description: 'Inspect corridor vectors, create chainage segments and independent buffer-ring units, or summarize zonal tables. inspect: source or geometryAsset; prepare_units: source/geometryAsset plus segmentKm and rings, with optional direction/startKm/endKm/workingCrs; summarize: table plus optional groupBy/valueColumn/weightColumn/pixelCountColumn. Local files require projectRoot inside allowedRoots. Writes generated units only under the configured stateRoot; source projects are read-only. Supports GeoJSON/Shapefile/GeoPackage and Earth Engine FeatureCollection assets.',
    parameters: {
      action: { type: 'string', required: true, enum: ['inspect', 'prepare_units', 'summarize'], description: 'Corridor operation.' },
      projectRoot: { type: 'string', description: 'Absolute project root inside allowedRoots; required for local source files and local tables.' },
      source: { type: 'string', description: 'Local vector path relative to projectRoot (GeoJSON, Shapefile, GeoPackage, etc.).' },
      geometryAsset: { type: 'string', description: 'Earth Engine FeatureCollection asset id, for example projects/my-project/assets/road.' },
      sourceCrs: { type: 'string', description: 'Source CRS when the vector has no CRS metadata, for example EPSG:4326.' },
      workingCrs: { type: 'string', description: 'Metric CRS used for length and buffers; defaults to EPSG:32645.' },
      direction: { type: 'string', enum: ['original', 'north_to_south', 'south_to_north'], description: 'Optional chainage direction; defaults to original.' },
      startKm: { type: 'number', description: 'Start chainage in km; defaults to 0.' },
      endKm: { type: 'number', description: 'End chainage in km; defaults to full line length.' },
      segmentKm: { type: 'number', description: 'Segment length in km; defaults to 10.' },
      rings: { type: 'array', items: { type: 'number' }, description: 'Strictly increasing independent ring outer distances in km, for example [2,4,6,8,10].' },
      table: { type: 'string', description: 'CSV path relative to projectRoot, or state:// path under stateRoot; required for summarize.' },
      groupBy: { type: 'string', enum: ['ring', 'segment', 'both'], description: 'Grouping for summarize; defaults to ring.' },
      valueColumn: { type: 'string', description: 'Numeric value column; defaults to value.' },
      weightColumn: { type: 'string', description: 'Optional non-negative weight column for weighted means.' },
      pixelCountColumn: { type: 'string', description: 'Optional pixel-count column summed per group.' },
      limit: { type: 'integer', description: 'Maximum groups returned by summarize, from 1 to 5000; defaults to 1000.' },
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
      geeProject: config.geeProject,
      maxHashBytes: config.maxHashBytes,
    }, config, exec.signal)),
  })))
}
