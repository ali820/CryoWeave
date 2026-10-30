import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/cryo-research-plugins/plugins/corridor-analysis/.venv/Scripts/python.exe',
  geeProject: 'ee-my-road',
  stateRoot: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/.cryo-state/corridor',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 180000,
  maxHashBytes: 268435456,
})
const tool = tools.get('corridor_analysis')
const exec = { signal: AbortSignal.timeout(170000) }
const call = async (args) => {
  const result = await tool.execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}

const inspected = await call({ action: 'inspect', geometryAsset: 'projects/ee-my-road/assets/G217duku_all_line', workingCrs: 'EPSG:32645' })
const prepared = await call({
  action: 'prepare_units',
  geometryAsset: 'projects/ee-my-road/assets/G217duku_all_line',
  workingCrs: 'EPSG:32645',
  direction: 'north_to_south',
  startKm: 0,
  endKm: 120,
  segmentKm: 10,
  rings: [2, 4, 6, 8, 10],
})
if (prepared.summary.units !== 60 || prepared.summary.segments !== 12) throw new Error('Unexpected unit count')
const summarized = await call({ action: 'summarize', table: prepared.statePaths.unitsTable, valueColumn: 'ring_area_km2', groupBy: 'ring' })
if (summarized.summary.groups !== 5 || summarized.summary.inputRows !== 60) throw new Error('Unexpected summary count')
console.log(JSON.stringify({
  ok: true,
  routeLengthKm: inspected.lengthKm,
  batchId: prepared.batchId,
  units: prepared.summary.units,
  ringGroups: summarized.summary.groups,
  ring01AreaKm2: summarized.rows[0].sum,
  statePaths: prepared.statePaths,
}))
