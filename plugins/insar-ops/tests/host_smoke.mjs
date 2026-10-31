import { existsSync } from 'node:fs'
import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/cryo-research-plugins/plugins/insar-ops/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/.cryo-state/insar',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 180000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(170000) }
const call = async (args) => {
  const result = await tools.get('insar_ops').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const result = await call({
  action: 'analyze',
  projectRoot: 'E:/文章同步/26-镇原/镇原县_InSAR成果包_轻量版_20260818',
  velocity: '镇原县_LOS速率_冬春季_202301-202608.tif',
  coherence: '镇原县_时间相干性.tif',
  velocityThreshold: 10,
  coherenceThreshold: 0.7,
  minAreaPixels: 20,
  maxClusters: 1000,
})
if (result.summary.validPixels !== 49632 || result.summary.clusterCount !== 263) throw new Error('Unexpected InSAR product statistics')
if (!existsSync(result.paths.clusters) || !existsSync(result.paths.summary) || !existsSync(result.paths.manifest)) throw new Error('Missing InSAR outputs')
console.log(JSON.stringify({ ok: true, batchId: result.batchId, validPixels: result.summary.validPixels, clusterCount: result.summary.clusterCount, totalAnomalyAreaKm2: result.summary.totalAnomalyAreaKm2, topClusters: result.rows.slice(0, 3) }))
