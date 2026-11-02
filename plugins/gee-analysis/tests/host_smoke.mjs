import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_GEE_ANALYSIS_PYTHON?.trim()
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

const temporary = await mkdtemp(join(tmpdir(), 'cryo-gee-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  const tools = new Map()
  const ctx = {
    effect: (callback) => callback(),
    tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
  }
  apply(ctx, {
    pythonPath: selectPython(),
    stateRoot,
    allowedRoots: [projectRoot],
    timeoutMs: 5000,
  })
  assert.deepEqual([...tools.keys()].sort(), ['gee_analyze', 'gee_corridor_analyze', 'gee_status'])

  // Shadow earthengine-api with a temporary module whose initialization fails
  // immediately. This reaches the real worker without credentials or network.
  const offlineModules = join(temporary, 'offline-python')
  await mkdir(offlineModules, { recursive: true })
  await writeFile(join(offlineModules, 'ee.py'), [
    '__version__ = "offline-probe"',
    'def Initialize(**kwargs):',
    '    raise RuntimeError("offline GEE initialization probe")',
    '',
  ].join('\n'), 'utf8')
  const previousPythonPath = process.env.PYTHONPATH
  process.env.PYTHONPATH = [offlineModules, previousPythonPath].filter(Boolean).join(delimiter)
  let failure
  try {
    const raw = await tools.get('gee_analyze').execute({
      operation: 'collection_summary',
      collection: 'COPERNICUS/S2_SR_HARMONIZED',
      startDate: '2024-01-01',
      endDate: '2024-01-02',
    }, { signal: AbortSignal.timeout(5000) })
    failure = JSON.parse(raw)
  } finally {
    if (previousPythonPath === undefined) delete process.env.PYTHONPATH
    else process.env.PYTHONPATH = previousPythonPath
  }
  assert.equal(failure.ok, false)
  assert.equal(failure.error?.code, 'GEE_AUTH_OR_PROJECT_FAILED')
  assert.match(failure.error.message, /offline GEE initialization probe/)

  // A pre-aborted execution must reject without spawning Python or contacting GEE.
  await assert.rejects(
    tools.get('gee_status').execute({}, { signal: AbortSignal.abort(new Error('offline cancellation probe')) }),
    /offline cancellation probe/,
  )

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    registered: [...tools.keys()].sort(),
    errorProtocol: failure.error.code,
    cancellation: 'rejected-before-spawn',
    onlineCoverage: 'manual integration_smoke.mjs only',
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
