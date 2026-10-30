import { spawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'cryo-research-evidence'
export const inject = ['tools']

const workerPath = fileURLToPath(new URL('./evidence_worker.py', import.meta.url))
const MAX_OUTPUT_BYTES = 4 * 1024 * 1024
const MAX_ERROR_BYTES = 32 * 1024

function failure(code, message) {
  return { ok: false, error: { code, message } }
}

function resolveConfig(config) {
  const pythonPath = config.pythonPath?.trim() || 'python'
  const timeoutMs = config.timeoutMs ?? 120000
  const maxHashBytes = config.maxHashBytes ?? 268435456
  const allowedRoots = config.allowedRoots
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 600000) {
    throw new Error('research-evidence: timeoutMs must be an integer from 1000 to 600000')
  }
  if (!Number.isInteger(maxHashBytes) || maxHashBytes < 1024 || maxHashBytes > 2147483648) {
    throw new Error('research-evidence: maxHashBytes must be an integer from 1024 to 2147483648')
  }
  if (!Array.isArray(allowedRoots) || allowedRoots.length === 0 || allowedRoots.some((root) => typeof root !== 'string' || !isAbsolute(root))) {
    throw new Error('research-evidence: allowedRoots must be a non-empty array of absolute paths')
  }
  return { pythonPath, timeoutMs, maxHashBytes, allowedRoots }
}

function runWorker(request, config, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('Evidence audit was cancelled'))
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
      stopWith(failure('EVIDENCE_TIMEOUT', `Evidence audit exceeded ${config.timeoutMs} ms. Reduce maxFiles, checks, or the project scope.`))
    }, config.timeoutMs)

    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > MAX_OUTPUT_BYTES) {
        stopWith(failure('EVIDENCE_OUTPUT_TOO_LARGE', 'Audit output exceeded 4 MiB. Reduce maxFindings or the project scope.'))
      }
    })
    child.stderr.on('data', (chunk) => {
      if (Buffer.byteLength(stderr, 'utf8') < MAX_ERROR_BYTES) stderr += chunk
    })
    child.stdin.on('error', () => {})
    child.on('error', (error) => {
      finish(resolve, failure('EVIDENCE_PYTHON_START_FAILED', `Cannot start ${config.pythonPath}: ${error.message}`))
    })
    child.on('close', (code) => {
      if (aborted) {
        finish(reject, signal?.reason ?? new Error('Evidence audit was cancelled'))
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
        finish(resolve, failure('EVIDENCE_WORKER_FAILED', `Evidence worker exited with code ${code ?? 'unknown'} and returned no valid JSON.${diagnostic ? ` ${diagnostic}` : ''}`))
      }
    })
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

export function apply(ctx, rawConfig = {}) {
  const config = resolveConfig(rawConfig)
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'research_evidence_audit',
    description: 'Read-only audit of a research project. Find CURRENT_AUTHORITY.md, verify figure *.export.json file existence/bytes/SHA-256, inspect claim_evidence_map*.csv required fields and gates, and summarize numeric_conflict_register.csv. Use an absolute projectRoot inside the configured allowedRoots. Does not infer scientific truth, scan manuscript prose, modify files, or access the network.',
    parameters: {
      projectRoot: { type: 'string', required: true, description: 'Absolute project directory to audit. It must be inside the plugin allowedRoots.' },
      checks: {
        type: 'array',
        items: { type: 'string', enum: ['authority', 'figures', 'claims', 'conflicts'] },
        description: 'Optional subset of checks. Defaults to all four checks.',
      },
      maxFiles: { type: 'integer', description: 'Maximum directory entries to scan, from 1 to 100000; defaults to 20000.' },
      maxFindings: { type: 'integer', description: 'Maximum returned findings, from 1 to 500; defaults to 100.' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: config.timeoutMs,
    isConcurrencySafe: true,
    execute: async (args, exec) => JSON.stringify(await runWorker({
      ...args,
      allowedRoots: config.allowedRoots,
      maxHashBytes: config.maxHashBytes,
    }, config, exec.signal)),
  })))
}
