import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Desktop/CryoWeave/plugins/field-multisource/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Desktop/CryoWeave-state/field',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 120000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(110000) }
const call = async (args) => {
  const result = await tools.get('field_multisource').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const projectRoot = 'E:/文章同步/4-field_observation'
const common = { projectRoot, headerRow: 2, timeColumnA: 'Unnamed: 0', timeColumnB: 'Unnamed: 0', valueColumnA: '0.5', valueColumnB: '0.5' }
const inspected = await call({ action: 'inspect', projectRoot, source: '2-数据及制图/2-地温数据/2309数据/上游日平均地温（S）.xls', sheetName: '9A', headerRow: 2, timeColumn: 'Unnamed: 0', valueColumns: ['0.5'] })
if (inspected.summary.rows !== 1373) throw new Error('Unexpected source row count')
const quality = await call({ action: 'qc', projectRoot, source: '2-数据及制图/2-地温数据/2309数据/上游日平均地温（S）.xls', sheetName: '9A', headerRow: 2, timeColumn: 'Unnamed: 0', valueColumns: ['0.5'], minValue: -50, maxValue: 50, maxGapHours: 72 })
if (quality.summary.qaStatus !== 'clean') throw new Error('Unexpected field QA result')
const aligned = await call({ action: 'align', ...common, sourceA: '2-数据及制图/2-地温数据/2309数据/上游日平均地温（S）.xls', sourceB: '2-数据及制图/2-地温数据/2309数据/下游日平均地温（X）.xls', sourceASheet: '9A', sourceBSheet: '11A', maxTimeDiffSeconds: 0, maxPairs: 5000 })
if (aligned.summary.matchedPairs !== 1273) throw new Error('Unexpected pair count')
const compared = await call({ action: 'compare', projectRoot, table: aligned.statePaths.pairs, columnA: 'valueA', columnB: 'valueB' })
if (Math.abs(compared.summary.rmse - 2.1649674519505946) > 0.000001) throw new Error('Unexpected comparison metrics')
console.log(JSON.stringify({ ok: true, batchId: aligned.batchId, rows: inspected.summary.rows, matchedPairs: aligned.summary.matchedPairs, qaStatus: quality.summary.qaStatus, comparison: compared.summary }))
