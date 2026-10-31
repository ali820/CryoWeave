import { existsSync } from 'node:fs'
import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Desktop/CryoWeave/plugins/lake-mapper/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Desktop/CryoWeave-state/lakes',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 180000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(170000) }
const call = async (args) => {
  const result = await tools.get('lake_mapper').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const inspected = await call({ action: 'inspect', projectRoot: 'E:/文章同步/25-湖泊连续性分析', source: '02_水体提取/data/lake_polys.geojson' })
if (inspected.featureCount !== 4) throw new Error('Expected four lake features')
const result = await call({
  action: 'morphology',
  projectRoot: 'E:/文章同步/25-湖泊连续性分析',
  source: '02_水体提取/data/lake_polys.geojson',
  workingCrs: 'EPSG:32645',
  minAreaKm2: 0.001,
  limit: 10,
})
if (result.summary.polygons !== 4 || result.summary.totalAreaKm2 <= 0) throw new Error('Unexpected morphology result')
for (const row of result.rows) {
  if (Math.abs(row.centroid[0]) > 180 || Math.abs(row.centroid[1]) > 90) throw new Error('Centroid is not WGS84')
}
if (!existsSync(result.paths.waterPolygons) || !existsSync(result.paths.morphology) || !existsSync(result.paths.manifest)) throw new Error('Missing lake output')
console.log(JSON.stringify({ ok: true, batchId: result.batchId, polygons: result.summary.polygons, totalAreaKm2: result.summary.totalAreaKm2, metrics: result.rows.map((row) => ({ lake: row.lake_id, area: row.area_km2, perimeter: row.perimeter_km, centroid: row.centroid, shapeIndex: row.shapeIndex, fractalDimension: row.fractalDimension })), statePaths: result.statePaths }))
