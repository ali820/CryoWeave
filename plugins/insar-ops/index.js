import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-insar-ops'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./insar_worker.py', import.meta.url))
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
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) throw new Error('insar-ops: invalid timeoutMs')
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) throw new Error('insar-ops: invalid maxHashBytes')
  if (typeof stateRoot !== 'string' || !isAbsolute(stateRoot)) throw new Error('insar-ops: stateRoot must be absolute')
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) throw new Error('insar-ops: allowedRoots must contain absolute paths')
  return { pythonPath, timeoutMs, maxHashBytes, stateRoot, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('InSAR operation was cancelled'))
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
    const timer = setTimeout(() => stopWith(failure('INSAR_TIMEOUT', `InSAR operation exceeded ${config.timeoutMs} ms.`)), config.timeoutMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) stopWith(failure('INSAR_OUTPUT_TOO_LARGE', 'InSAR output exceeded 8 MiB.'))
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => finish(resolve, failure('INSAR_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`)))
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('InSAR operation was cancelled'))
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
        finish(resolve, failure('INSAR_WORKER_FAILED', `InSAR worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'insar_ops',
    description: 'Inspect Sentinel-1 archives, plan interferogram pairs, analyze velocity/coherence products for anomaly clusters, and verify reference versions and file hashes. Local files require projectRoot inside allowedRoots. Outputs are written only under stateRoot. Anomaly clusters are QA candidates, not automatic landslide or damage classifications.',
    parameters: {
      action: { type: 'string', required: true, enum: ['inspect', 'plan', 'analyze', 'verify'], description: 'Archive inspection, pair planning, product analysis, or reference verification.' },
      projectRoot: { type: 'string', required: true, description: 'Absolute project root inside allowedRoots.' },
      archiveRoot: { type: 'string', description: 'Archive directory for inspect/plan, relative to projectRoot or absolute inside it.' },
      pattern: { type: 'string', description: 'Archive file glob; defaults to *.zip.' },
      maxFiles: { type: 'integer', description: 'Maximum archive files to inspect, from 1 to 100000; defaults to 2000.' },
      maxTemporalBaselineDays: { type: 'integer', description: 'Maximum temporal baseline for pairs; defaults to 48 days.' },
      maxConnections: { type: 'integer', description: 'Maximum subsequent scenes per master; defaults to 4.' },
      maxPairs: { type: 'integer', description: 'Maximum generated pairs; defaults to 5000.' },
      sameTrack: { type: 'boolean', description: 'Require pairs to share track; defaults to true.' },
      velocity: { type: 'string', description: 'Velocity GeoTIFF for analyze, relative to projectRoot or absolute inside it.' },
      coherence: { type: 'string', description: 'Coherence GeoTIFF for analyze.' },
      velocityScale: { type: 'number', description: 'Scale applied to velocity; defaults to 1.' },
      coherenceScale: { type: 'number', description: 'Scale applied to coherence; defaults to 1.' },
      velocityThreshold: { type: 'number', description: 'Absolute velocity threshold for anomaly pixels; defaults to 10 mm/yr.' },
      coherenceThreshold: { type: 'number', description: 'Minimum temporal coherence from 0 to 1; defaults to 0.7.' },
      minAreaPixels: { type: 'integer', description: 'Minimum connected anomalous pixels per cluster; defaults to 20.' },
      maxClusters: { type: 'integer', description: 'Maximum clusters returned; defaults to 1000.' },
      workingCrs: { type: 'string', description: 'Metric CRS for cluster area; defaults to EPSG:32645.' },
      referenceManifest: { type: 'string', description: 'Reference manifest JSON with reference_version and products[{path,sha256}].' },
      expectedReferenceVersion: { type: 'string', description: 'Optional expected reference version for stale-product detection.' },
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
