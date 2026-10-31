import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/cryo-research-plugins/plugins/hydro-event/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/.cryo-state/hydro',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 180000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(170000) }
const call = async (args) => {
  const result = await tools.get('hydro_event').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const projectRoot = 'E:/文章同步/25-湖泊连续性分析'
const storage = await call({ action: 'storage', projectRoot, hypsometry: '04_水量平衡/output/hypsometry_卓乃湖_AW3D30.csv', levelM: 4760 })
if (storage.volumeKm3 <= 0) throw new Error('Storage interpolation failed')
const breach = await call({ action: 'breach', projectRoot, hypsometry: '04_水量平衡/output/hypsometry_卓乃湖_AW3D30.csv', preLevelM: 4760, postLevelM: 4744.6, durationDays: 22, widthM: 40, dischargeCoefficient: 1.0, headM: 15.4 })
if (Math.abs(breach.releaseKm3 - 3.139522) > 0.00001 || breach.scenarioOnly !== true) throw new Error('Unexpected breach result')
const balance = await call({ action: 'water_balance', projectRoot, components: '04_水量平衡/output/closure_terms.csv', outcomeKm3: 3.073, outcomeTerm: '卓乃湖释放', nonSourceTerms: ['本地径流', '湖面直接降水'] })
if (Math.abs(balance.summary.closureRatio - 0.8717865278229741) > 0.000001 || Math.abs(balance.summary.residualKm3 - 0.394) > 0.000001) throw new Error('Unexpected closure result')
console.log(JSON.stringify({ ok: true, storageVolumeKm3: storage.volumeKm3, releaseKm3: breach.releaseKm3, meanOutflowM3s: breach.meanOutflowM3s, peakScenarioM3s: breach.weirScenario.peakOutflowM3s, closureRatio: balance.summary.closureRatio, residualKm3: balance.summary.residualKm3 }))
