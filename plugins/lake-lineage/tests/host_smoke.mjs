import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_LAKE_LINEAGE_PYTHON?.trim()
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
const temporary = await mkdtemp(join(tmpdir(), 'cryo-lineage-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  const fixture = spawnSync(pythonPath, ['-c', `
import sys
import geopandas as gpd
from pathlib import Path
root = Path(sys.argv[1])
def polygon(x):
    return {'type': 'Polygon', 'coordinates': [[[x, 4700000], [x + 1000, 4700000], [x + 1000, 4701000], [x, 4701000], [x, 4700000]]]}
def write(path, geometries):
    features = [{'type': 'Feature', 'geometry': geometry, 'properties': {}} for geometry in geometries]
    gpd.GeoDataFrame.from_features(features, crs='EPSG:32645').to_file(path, driver='GeoJSON')
write(root / 'lakes_2020.geojson', [polygon(500000)])
write(root / 'lakes_2021.geojson', [polygon(500000), polygon(504000)])
write(root / 'roads.geojson', [{'type': 'LineString', 'coordinates': [[500000, 4700000], [501000, 4700000]]}])
`, projectRoot], { encoding: 'utf8', timeout: 60000 })
  assert.equal(fixture.status, 0, fixture.stderr || fixture.error?.message)

  const tools = new Map()
  const ctx = {
    effect: (callback) => callback(),
    tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
  }
  apply(ctx, {
    pythonPath,
    stateRoot,
    allowedRoots: [projectRoot],
    timeoutMs: 60000,
    maxHashBytes: 1024 * 1024,
  })
  const tool = tools.get('lake_lineage')
  assert.equal(typeof tool?.execute, 'function', 'lake_lineage was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const yearFiles = [2020, 2021].map((year) => ({ year, path: `lakes_${year}.geojson` }))
  const inspected = await call({ action: 'inspect', projectRoot, yearFiles: yearFiles.slice(0, 1), workingCrs: 'EPSG:32645' })
  assert.equal(inspected.years[0].nodes, 1)
  const built = await call({ action: 'build', projectRoot, yearFiles, workingCrs: 'EPSG:32645', dominantOverlap: 0.95, minorOverlap: 0.001 })
  assert.equal(built.summary.lineages, 2)
  assert.equal(built.summary.events, 1)
  assert.equal(built.eventTypes.new, 1)
  assert.ok(existsSync(built.paths.cohort) && existsSync(built.paths.events))

  const colocated = await call({
    action: 'colocate', projectRoot, cohort: built.statePaths.cohort,
    transport: 'roads.geojson', workingCrs: 'EPSG:32645', limit: 10,
  })
  assert.equal(colocated.summary.count, 2)
  assert.ok(colocated.rows.every((row) => Number.isFinite(row.nearestDistanceKm)))
  assert.ok(colocated.rows.some((row) => row.within1Km))

  const invalid = await raw({ action: 'inspect', projectRoot, yearFiles: [{ year: 2020, path: '../outside.geojson' }] })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'LINEAGE_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    lineages: built.summary.lineages,
    events: built.summary.events,
    colocationCount: colocated.summary.count,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
