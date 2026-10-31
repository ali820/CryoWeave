import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-gee-analysis'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./gee_worker.py', import.meta.url))
const MAX_OUTPUT_BYTES = 4 * 1024 * 1024
const MAX_ERROR_BYTES = 32 * 1024

function failure(code, message) {
  return { ok: false, error: { code, message } }
}

function resolveConfig(config) {
  const pythonPath = config.pythonPath?.trim() || 'python'
  const project = config.project?.trim() || process.env.DSH_GEE_PROJECT || process.env.GEE_PROJECT || process.env.GOOGLE_CLOUD_PROJECT || undefined
  const apiUrl = config.apiUrl?.trim() || undefined
  const timeoutMs = config.timeoutMs ?? 240000
  const stateRoot = config.stateRoot
  const allowedRoots = config.allowedRoots
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) {
    throw new Error('GEE plugin: timeoutMs must be an integer from 1000 to 600000')
  }
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) {
    throw new Error('GEE plugin: stateRoot must be an absolute path')
  }
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) {
    throw new Error('GEE plugin: allowedRoots must be a non-empty array of absolute paths')
  }
  return { pythonPath, project, apiUrl, timeoutMs, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('GEE analysis was cancelled'))
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
      stopWith(failure('GEE_TIMEOUT', `Earth Engine analysis exceeded ${config.timeoutMs} ms. Narrow the date range, reduce limit, or increase the plugin timeout.`))
    }, config.timeoutMs)

    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) {
        stopWith(failure('GEE_OUTPUT_TOO_LARGE', 'The worker output exceeded 2 MiB. Reduce the number of requested rows.'))
      }
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => {
      finish(resolve, failure('GEE_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`))
    })
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('GEE analysis was cancelled'))
        return
      }
      if (terminalFailure) {
        finish(resolve, terminalFailure)
        return
      }
      const text = stdout.trim()
      try {
        const parsed = JSON.parse(text)
        finish(resolve, parsed)
      } catch {
        const diagnostic = stderr.trim().slice(-8000)
        finish(resolve, failure(
          'GEE_WORKER_FAILED',
          `GEE worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`,
        ))
      }
    })

    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

const analysisParameters = {
  project: {
    type: 'string',
    description: 'Optional GEE-enabled Google Cloud project id for this call; overrides the plugin and environment defaults.',
  },
  operation: {
    type: 'string',
    required: true,
    enum: ['collection_summary', 'region_stats', 'timeseries', 'ndvi_timeseries', 'ndvi_stats'],
    description: 'Analysis to run. NDVI operations use COPERNICUS/S2_SR_HARMONIZED.',
  },
  geometry: {
    type: 'object',
    additionalProperties: true,
    description: 'GeoJSON geometry or Feature. Required unless longitude and latitude are supplied.',
  },
  geometryAsset: {
    type: 'string',
    description: 'Earth Engine FeatureCollection asset id, for example projects/my-project/assets/study_region. Optional bufferMeters also applies to this geometry.',
  },
  longitude: { type: 'number', description: 'Point longitude in WGS84 degrees; use together with latitude.' },
  latitude: { type: 'number', description: 'Point latitude in WGS84 degrees; use together with longitude.' },
  bufferMeters: { type: 'number', description: 'Buffer around a point or geometryAsset in meters; point buffers default to 1000.' },
  collection: { type: 'string', description: 'Earth Engine ImageCollection id. Required for non-NDVI operations.' },
  bands: { type: 'array', items: { type: 'string' }, description: 'Bands to analyse. Required for region_stats and timeseries.' },
  startDate: { type: 'string', required: true, description: 'Inclusive UTC start date in YYYY-MM-DD format.' },
  endDate: { type: 'string', required: true, description: 'Exclusive UTC end date in YYYY-MM-DD format.' },
  stat: {
    type: 'string',
    enum: ['mean', 'median', 'min', 'max', 'sum', 'stdDev', 'variance', 'count'],
    description: 'Regional reducer; defaults to mean.',
  },
  composite: {
    type: 'string',
    enum: ['median', 'mean', 'first', 'mosaic'],
    description: 'Collection composite used by region_stats; defaults to median.',
  },
  scale: { type: 'number', description: 'Reduction scale in meters; defaults to 30, or 10 for NDVI.' },
  maxPixels: { type: 'number', description: 'Maximum pixels in a reduction; defaults to 1e9.' },
  bestEffort: { type: 'boolean', description: 'Allow Earth Engine to use a coarser scale when needed; defaults to false.' },
  tileScale: { type: 'number', description: 'Earth Engine tileScale from 1 to 16; defaults to 1.' },
  crs: { type: 'string', description: 'Optional output CRS, for example EPSG:4326.' },
  cloudMax: { type: 'number', description: 'Maximum CLOUDY_PIXEL_PERCENTAGE from 0 to 100. NDVI defaults to 60.' },
  maskClouds: { type: 'boolean', description: 'For NDVI, mask clouds, cloud shadows, cirrus, and snow using SCL and QA60; defaults to true.' },
  limit: { type: 'integer', description: 'Time-series image cap (default 100, maximum 500), region composite cap, or summary id sample cap.' },
  dropNull: { type: 'boolean', description: 'Omit time-series rows whose requested statistic is null; defaults to true.' },
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  const invoke = async (request, exec) => {
    const requestedProject = request.project?.trim()
    return JSON.stringify(await runWorker({
      ...request,
      project: requestedProject || config.project,
      apiUrl: config.apiUrl,
      stateRoot: config.stateRoot,
      allowedRoots: config.allowedRoots,
    }, config, exec.signal))
  }

  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'gee_status',
    description: 'Check the isolated Python environment, Earth Engine API initialization, configured project, and a minimal live Earth Engine request. Does not expose credentials.',
    parameters: {
      project: {
        type: 'string',
        description: 'Optional GEE-enabled Google Cloud project id for this check; overrides the plugin and environment defaults.',
      },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: config.timeoutMs,
    isConcurrencySafe: true,
    execute: (_args, exec) => invoke({ operation: 'status' }, exec),
  })))

  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'gee_analyze',
    description: 'Run a bounded Google Earth Engine analysis and return JSON results immediately. Supports ImageCollection summaries, regional band statistics, generic time series, and Sentinel-2 NDVI statistics/time series. Use geometry as GeoJSON or a WGS84 point with an optional buffer. Dates use an inclusive start and exclusive end. This tool does not execute arbitrary Python or JavaScript.',
    parameters: analysisParameters,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: config.timeoutMs,
    isConcurrencySafe: true,
    execute: (args, exec) => invoke(args, exec),
  })))

  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'gee_corridor_analyze',
    description: 'Compute Sentinel-2 NDVI for each corridor segment × independent buffer ring from units.geojson produced by corridor_analysis. Supports state:// paths under the GEE stateRoot or local files inside allowedRoots. Returns up to 100 rows plus a CSV and manifest under stateRoot. Dates use an inclusive start and exclusive end. Does not execute arbitrary code.',
    parameters: {
      table: { type: 'string', required: true, description: 'Unit GeoJSON path, for example state://<batchId>/units.geojson or a local path inside allowedRoots.' },
      projectRoot: { type: 'string', description: 'Absolute project root required only for local unit files.' },
      project: { type: 'string', description: 'Optional GEE-enabled Google Cloud project id for this call.' },
      startDate: { type: 'string', required: true, description: 'Inclusive UTC start date in YYYY-MM-DD format.' },
      endDate: { type: 'string', required: true, description: 'Exclusive UTC end date in YYYY-MM-DD format.' },
      unitIds: { type: 'array', items: { type: 'string' }, description: 'Optional segment_id filter.' },
      ringIds: { type: 'array', items: { type: 'string' }, description: 'Optional ring_id filter.' },
      limit: { type: 'integer', description: 'Maximum units to process, from 1 to 500; defaults to 60.' },
      imageLimit: { type: 'integer', description: 'Optional cap on source images before median compositing, from 1 to 500.' },
      stat: { type: 'string', enum: ['mean', 'median', 'min', 'max', 'sum', 'stdDev', 'variance'], description: 'Zonal reducer; defaults to mean.' },
      scale: { type: 'number', description: 'Reduction scale in meters; defaults to 30.' },
      cloudMax: { type: 'number', description: 'Maximum CLOUDY_PIXEL_PERCENTAGE; defaults to 60.' },
      maskClouds: { type: 'boolean', description: 'Mask clouds, shadows, cirrus, and snow using SCL and QA60; defaults to true.' },
      tileScale: { type: 'number', description: 'Earth Engine tileScale from 1 to 16; defaults to 1.' },
      crs: { type: 'string', description: 'Optional output CRS, for example EPSG:4326.' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: config.timeoutMs,
    isConcurrencySafe: false,
    execute: (args, exec) => invoke({ ...args, operation: 'corridor_ndvi_zonal' }, exec),
  })))
}
