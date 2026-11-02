import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_LAKE_MAPPER_PYTHON?.trim()
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

const temporary = await mkdtemp(join(tmpdir(), 'cryo-lake-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  const square = (x, y, size) => ({
    type: 'Polygon',
    coordinates: [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]],
  })
  await writeFile(join(projectRoot, 'lakes.geojson'), JSON.stringify({
    type: 'FeatureCollection',
    features: [
      { type: 'Feature', properties: { lake_id: 'alpha' }, geometry: square(84, 43, 0.01) },
      { type: 'Feature', properties: { lake_id: 'beta' }, geometry: square(84.03, 43.03, 0.02) },
    ],
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
  const tool = tools.get('lake_mapper')
  assert.equal(typeof tool?.execute, 'function', 'lake_mapper was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const common = { projectRoot, source: 'lakes.geojson', workingCrs: 'EPSG:32645' }
  const inspected = await call({ action: 'inspect', ...common })
  assert.equal(inspected.featureCount, 2)
  const result = await call({ action: 'morphology', ...common, minAreaKm2: 0.001, limit: 10 })
  assert.equal(result.summary.polygons, 2)
  assert.ok(result.summary.totalAreaKm2 > 0)
  assert.ok(result.rows.every((row) => Math.abs(row.centroid[0]) <= 180 && Math.abs(row.centroid[1]) <= 90))
  assert.ok(result.rows.every((row) => row.shapeIndex >= 1))
  assert.ok(existsSync(result.paths.waterPolygons) && existsSync(result.paths.morphology) && existsSync(result.paths.manifest))

  const invalid = await raw({ action: 'inspect', projectRoot, source: '../outside.geojson' })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'LAKE_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    polygons: result.summary.polygons,
    totalAreaKm2: result.summary.totalAreaKm2,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
