import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_RESEARCH_RUN_PYTHON?.trim()
  if (configured) return configured
  const pluginRoot = fileURLToPath(new URL('../', import.meta.url))
  const local = [join(pluginRoot, '.venv', 'Scripts', 'python.exe'), join(pluginRoot, '.venv', 'bin', 'python')].find(existsSync)
  if (local) return local
  for (const candidate of ['python', 'python3']) {
    const probe = spawnSync(candidate, ['--version'], { stdio: 'ignore' })
    if (!probe.error && probe.status === 0) return candidate
  }
  return 'python'
}

const temporary = await mkdtemp(join(tmpdir(), 'cryo-run-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  await writeFile(join(projectRoot, 'input.csv'), 'a,b\n1,2\n', 'utf8')

  const tools = new Map()
  const ctx = {
    effect: (callback) => callback(),
    tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
  }
  apply(ctx, {
    pythonPath: selectPython(),
    allowedRoots: [projectRoot],
    stateRoot,
    timeoutMs: 60000,
    maxHashBytes: 1024 * 1024,
  })
  const tool = tools.get('research_run')
  assert.equal(typeof tool?.execute, 'function', 'research_run was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const created = await call({
    action: 'create', projectRoot, pipeline: 'offline-host-validation',
    config: { year: 2024, apiKey: 'must-not-persist' },
    inputs: ['input.csv'], steps: ['analysis'],
  })
  const runId = created.run.runId
  assert.equal(created.run.config.apiKey, '[REDACTED]')
  await call({ action: 'update_status', runId, status: 'running', step: 'analysis', note: 'offline Host lifecycle' })

  const artifact = join(projectRoot, 'result.json')
  await writeFile(artifact, '{"value":1}\n', 'utf8')
  await call({ action: 'register_artifacts', runId, artifacts: ['result.json'], status: 'complete', step: 'analysis' })
  const current = await call({ action: 'verify', runId })
  assert.equal(current.auditStatus, 'current')

  await writeFile(artifact, '{"value":2}\n', 'utf8')
  const stale = await call({ action: 'verify', runId })
  assert.equal(stale.auditStatus, 'stale')
  assert.equal(stale.findings[0]?.code, 'ARTIFACT_SHA256_MISMATCH')
  const listed = await call({ action: 'list', pipeline: 'offline-host-validation', limit: 5 })
  assert.equal(listed.total, 1)

  const invalid = await raw({ action: 'update_status', runId, status: 'planned' })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'RUN_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    lifecycle: 'create>running>complete',
    current: current.auditStatus,
    stale: stale.auditStatus,
    listed: listed.total,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
