import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_PHYSICS_SCENARIOS_PYTHON?.trim()
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

const pythonPath = selectPython()
const temporary = await mkdtemp(join(tmpdir(), 'cryo-physics-host-'))
const contextFor = (tools) => ({
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
})
try {
  const stateRoot = join(temporary, 'state')
  const tools = new Map()
  apply(contextFor(tools), { pythonPath, stateRoot, timeoutMs: 60000, maxHashBytes: 1024 * 1024 })
  const tool = tools.get('physics_scenarios')
  assert.equal(typeof tool?.execute, 'function', 'physics_scenarios was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const thermal = await call({
    action: 'thermal', conductivityWmK: 2, densityKgM3: 2400, heatCapacityJkgK: 960,
    meanSurfaceC: 14.2, amplitudeC: 12, periodDays: 1,
    depthsM: [0, 0.05, 0.1, 0.2], timesDays: [0, 0.25, 0.5, 0.75, 1],
  })
  assert.ok(Math.abs(thermal.summary.diffusivityM2PerDay - 0.075) < 1e-12)
  assert.equal(thermal.summary.rowCount, 20)
  assert.equal(thermal.scenarioOnly, true)
  assert.ok(existsSync(thermal.paths.table) && existsSync(thermal.paths.manifest))

  const ensemble = await call({
    action: 'ensemble', scenario: 'thermal',
    baseParameters: { depthM: 0.2, timeDays: 0.5 },
    parameterRanges: { conductivityWmK: [1.6, 2.4], amplitudeC: [10, 14] },
    samples: 20, randomState: 42,
  })
  assert.equal(ensemble.summary.samples, 20)
  assert.equal(ensemble.summary.rangeCheck, true)
  assert.ok(ensemble.summary.temperatureQuantilesC.p95 > ensemble.summary.temperatureQuantilesC.p05)

  const invalid = await raw({ action: 'thermal', conductivityWmK: -1 })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'PHYSICS_ERROR')

  // Force a deterministic slow Python startup to exercise the real Host timer.
  // PYTHONNOUSERSITE does not disable sitecustomize loaded from PYTHONPATH.
  const slowSite = join(temporary, 'slow-site')
  await mkdir(slowSite, { recursive: true })
  await writeFile(join(slowSite, 'sitecustomize.py'), 'import time\ntime.sleep(30)\n', 'utf8')
  const previousPythonPath = process.env.PYTHONPATH
  process.env.PYTHONPATH = [slowSite, previousPythonPath].filter(Boolean).join(delimiter)
  let timedOut
  try {
    const timeoutTools = new Map()
    apply(contextFor(timeoutTools), {
      pythonPath,
      stateRoot: join(temporary, 'timeout-state'),
      timeoutMs: 1000,
      maxHashBytes: 1024 * 1024,
    })
    const rawTimeout = await timeoutTools.get('physics_scenarios').execute(
      { action: 'thermal' },
      { signal: AbortSignal.timeout(10000) },
    )
    timedOut = JSON.parse(rawTimeout)
  } finally {
    if (previousPythonPath === undefined) delete process.env.PYTHONPATH
    else process.env.PYTHONPATH = previousPythonPath
  }
  assert.equal(timedOut.ok, false)
  assert.equal(timedOut.error?.code, 'PHYSICS_TIMEOUT')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    thermalRows: thermal.summary.rowCount,
    ensembleSamples: ensemble.summary.samples,
    errorProtocol: invalid.error.code,
    timeoutProtocol: timedOut.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
