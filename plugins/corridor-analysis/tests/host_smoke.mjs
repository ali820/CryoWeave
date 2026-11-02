import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_CORRIDOR_ANALYSIS_PYTHON?.trim()
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

const temporary = await mkdtemp(join(tmpdir(), 'cryo-corridor-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  await writeFile(join(projectRoot, 'line.geojson'), JSON.stringify({
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: {},
      geometry: { type: 'LineString', coordinates: [[84, 43], [84.1, 43.1]] },
    }],
  }), 'utf8')

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
  const tool = tools.get('corridor_analysis')
  assert.equal(typeof tool?.execute, 'function', 'corridor_analysis was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const common = { projectRoot, source: 'line.geojson', workingCrs: 'EPSG:32645' }
  const inspected = await call({ action: 'inspect', ...common })
  assert.equal(inspected.featureCount, 1)
  assert.ok(inspected.lengthKm > 0)

  const prepared = await call({ action: 'prepare_units', ...common, direction: 'north_to_south', segmentKm: 5, rings: [1, 2] })
  assert.equal(prepared.summary.ringsPerSegment, 2)
  assert.equal(prepared.summary.units, prepared.summary.segments * 2)
  assert.ok(existsSync(prepared.paths.units) && existsSync(prepared.paths.unitsTable))

  const summarized = await call({
    action: 'summarize',
    table: prepared.statePaths.unitsTable,
    valueColumn: 'ring_area_km2',
    groupBy: 'ring',
  })
  assert.equal(summarized.summary.groups, 2)
  assert.equal(summarized.summary.inputRows, prepared.summary.units)
  assert.ok(summarized.rows.every((row) => row.sum > 0))

  const invalid = await raw({ action: 'inspect', ...common, source: '../outside.geojson' })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'CORRIDOR_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    segments: prepared.summary.segments,
    units: prepared.summary.units,
    ringGroups: summarized.summary.groups,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
