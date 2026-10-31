import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Desktop/CryoWeave/plugins/lake-lineage/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Desktop/CryoWeave-state/lineage',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 240000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(230000) }
const call = async (args) => {
  const result = await tools.get('lake_lineage').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const projectRoot = 'E:/文章同步/20-qxp/qxp-lake-infrastructure-exposure/02_数据与方法'
const yearFiles = [2020, 2021, 2022, 2023].map((year) => ({ year, path: `data/raw/lake_tp/extracted/Lake_TP/${year}/Lake_TP_${year}.shp` }))
const inspected = await call({ action: 'inspect', projectRoot, yearFiles: yearFiles.slice(0, 1), workingCrs: 'ESRI:102025' })
if (!inspected.years?.[0]?.nodes) throw new Error('No lake polygons inspected')
const built = await call({ action: 'build', projectRoot, yearFiles, workingCrs: 'ESRI:102025', dominantOverlap: 0.95, minorOverlap: 0.001, maxPolygonsPerYear: 10000 })
if (built.summary.lineages < 1000 || built.summary.events < 100) throw new Error('Unexpected lineage result')
const colocated = await call({ action: 'colocate', projectRoot, cohort: built.statePaths.cohort, transport: 'data/processed/transport_qxp.gpkg', workingCrs: 'ESRI:102025', limit: 10 })
if (colocated.summary.count !== 10 || colocated.rows.some((row) => !Number.isFinite(row.nearestDistanceKm))) throw new Error('Unexpected co-location result')
console.log(JSON.stringify({ ok: true, batchId: built.batchId, lineages: built.summary.lineages, events: built.summary.events, eventTypes: built.eventTypes, colocationCount: colocated.summary.count, sample: colocated.rows.slice(0, 3).map((row) => ({ lineageId: row.lineageId, distanceKm: row.nearestDistanceKm, within10Km: row.within10Km })) }))
