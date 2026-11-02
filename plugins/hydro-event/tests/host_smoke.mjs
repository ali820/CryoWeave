import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_HYDRO_EVENT_PYTHON?.trim()
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

const temporary = await mkdtemp(join(tmpdir(), 'cryo-hydro-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  await writeFile(join(projectRoot, 'hypsometry.csv'), [
    'elev_m,area_below_km2,volume_km3',
    '0,1,0',
    '10,2,1',
    '20,3,3',
    '',
  ].join('\n'), 'utf8')
  await writeFile(join(projectRoot, 'components.csv'), [
    'term,central_km3,lo_km3,hi_km3',
    'inflow,2,1.5,2.5',
    'outflow,-0.5,-0.7,-0.3',
    'local,0.5,0.4,0.6',
    'release,3,2,4',
    '',
  ].join('\n'), 'utf8')

  const tools = new Map()
  const ctx = {
    effect: (callback) => callback(),
    tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
  }
  apply(ctx, {
    pythonPath: selectPython(),
    stateRoot,
    allowedRoots: [projectRoot],
    timeoutMs: 60000,
    maxHashBytes: 1024 * 1024,
  })
  const tool = tools.get('hydro_event')
  assert.equal(typeof tool?.execute, 'function', 'hydro_event was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const storage = await call({ action: 'storage', projectRoot, hypsometry: 'hypsometry.csv', levelM: 10 })
  assert.equal(storage.volumeKm3, 1)
  assert.equal(storage.areaKm2, 2)
  const breach = await call({
    action: 'breach', projectRoot, hypsometry: 'hypsometry.csv',
    preLevelM: 20, postLevelM: 0, durationDays: 2,
    widthM: 2, dischargeCoefficient: 1.7, headM: 1,
  })
  assert.equal(breach.releaseKm3, 3)
  assert.equal(breach.scenarioOnly, true)
  assert.ok(Math.abs(breach.weirScenario.peakOutflowM3s - 3.4) < 1e-12)
  const balance = await call({
    action: 'water_balance', projectRoot, components: 'components.csv',
    outcomeKm3: 1, outcomeTerm: 'release', nonSourceTerms: ['local'],
  })
  assert.equal(balance.summary.closureRatio, 1)
  assert.equal(balance.summary.residualKm3, 0)

  const invalid = await raw({ action: 'storage', projectRoot, hypsometry: '../outside.csv', levelM: 10 })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'HYDRO_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    storageVolumeKm3: storage.volumeKm3,
    releaseKm3: breach.releaseKm3,
    closureRatio: balance.summary.closureRatio,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
