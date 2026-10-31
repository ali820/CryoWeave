import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/cryo-research-plugins/plugins/model-experiments/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/.cryo-state/ml',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 180000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(170000) }
const call = async (args) => {
  const result = await tools.get('model_experiments').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const common = {
  projectRoot: 'E:/文章同步/20-qxp/qxp-lake-infrastructure-exposure',
  table: '02_数据与方法/data/outputs/p5_lake_tp_planimetric/audit/classifier_training.csv',
  featureColumns: ['log_area_km2', 'log_perimeter_km', 'compactness', 'convexity', 'elongation'],
  targetColumn: 'reference_label',
  task: 'classification',
  positiveLabel: 'lake',
  groupColumn: 'spatial_block',
  model: 'random_forest',
  cvFolds: 5,
  nEstimators: 100,
  randomState: 42,
}
const evaluated = await call({ action: 'evaluate', ...common })
if (evaluated.summary.rows !== 3923 || evaluated.summary.foldCount !== 5 || evaluated.summary.metrics.accuracy < 0.9) throw new Error('Unexpected model validation result')
if (evaluated.summary.leakageChecks.groupOverlapCount !== 0) throw new Error('Group leakage detected')
const important = await call({ action: 'importance', ...common, nRepeats: 5 })
if (!important.summary.notShap || important.summary.features[0].feature !== 'convexity') throw new Error('Unexpected importance result')
console.log(JSON.stringify({ ok: true, evaluationBatch: evaluated.batchId, accuracy: evaluated.summary.metrics.accuracy, rocAuc: evaluated.summary.metrics.rocAuc, baselineAccuracy: evaluated.summary.baseline.accuracy, topImportance: important.summary.features.slice(0, 3) }))
