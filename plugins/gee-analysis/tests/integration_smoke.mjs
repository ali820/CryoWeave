import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply as applyGee } from '../index.js'
import { apply as applyCorridor } from '../../corridor-analysis/index.js'
import { apply as applyRun } from '../../research-run/index.js'

// This is a manual ONLINE integration test, not part of offline Host smoke CI.
if (process.env.CRYO_GEE_INTEGRATION !== '1') {
  console.log(JSON.stringify({
    skipped: true,
    manual: true,
    reason: 'Set CRYO_GEE_INTEGRATION=1 with authenticated Earth Engine credentials to run the online integration test.',
  }))
  process.exit(0)
}

function selectPython(specificVariable) {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env[specificVariable]?.trim()
  if (configured) return configured
  for (const candidate of ['python', 'python3']) {
    const probe = spawnSync(candidate, ['--version'], { stdio: 'ignore' })
    if (!probe.error && probe.status === 0) return candidate
  }
  return 'python'
}

const project = process.env.CRYO_GEE_PROJECT?.trim()
const projectRoot = process.env.CRYO_GEE_PROJECT_ROOT?.trim()
const units = process.env.CRYO_GEE_UNITS?.trim()
assert.ok(project, 'CRYO_GEE_PROJECT is required for the manual online test')
assert.ok(projectRoot, 'CRYO_GEE_PROJECT_ROOT must identify the allowed local project root')
assert.ok(units, 'CRYO_GEE_UNITS must identify a units.geojson inside CRYO_GEE_PROJECT_ROOT')

const temporary = await mkdtemp(join(tmpdir(), 'cryo-gee-manual-'))
try {
  const stateRoot = join(temporary, 'state')
  const tools = new Map()
  const ctx = {
    effect: (callback) => callback(),
    tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
  }
  const common = { stateRoot, allowedRoots: [projectRoot], timeoutMs: 240000, maxHashBytes: 1024 * 1024 }
  applyGee(ctx, { ...common, pythonPath: selectPython('CRYO_GEE_ANALYSIS_PYTHON'), project })
  applyCorridor(ctx, { ...common, pythonPath: selectPython('CRYO_CORRIDOR_ANALYSIS_PYTHON'), geeProject: project })
  applyRun(ctx, { ...common, pythonPath: selectPython('CRYO_RESEARCH_RUN_PYTHON') })

  const call = async (name, args) => {
    const result = JSON.parse(await tools.get(name).execute(args, { signal: AbortSignal.timeout(240000) }))
    assert.equal(result.ok, true, `${name}: ${JSON.stringify(result)}`)
    return result
  }
  const zonal = await call('gee_corridor_analyze', {
    table: units,
    projectRoot,
    startDate: process.env.CRYO_GEE_START_DATE || '2024-06-01',
    endDate: process.env.CRYO_GEE_END_DATE || '2024-07-01',
    limit: 1,
    imageLimit: 2,
    scale: 30,
    stat: 'mean',
  })
  assert.equal(zonal.summary.outputRows, 1)
  assert.ok(existsSync(zonal.paths.csv) && existsSync(zonal.paths.manifest))

  const summarized = await call('corridor_analysis', {
    action: 'summarize',
    table: zonal.statePaths.csv,
    valueColumn: 'value',
    pixelCountColumn: 'pixel_count',
    groupBy: 'segment',
  })
  assert.equal(summarized.summary.groups, 1)

  const created = await call('research_run', {
    action: 'create',
    projectRoot,
    pipeline: 'manual-gee-ndvi-zonal',
    config: { scale: 30, stat: 'mean', online: true },
    steps: ['zonal_stats'],
  })
  const runId = created.run.runId
  await call('research_run', { action: 'update_status', runId, status: 'running', step: 'zonal_stats' })
  await call('research_run', {
    action: 'register_artifacts', runId,
    artifacts: [zonal.statePaths.csv], status: 'complete', step: 'zonal_stats',
  })
  const verified = await call('research_run', { action: 'verify', runId })
  assert.equal(verified.auditStatus, 'current')

  console.log(JSON.stringify({ ok: true, manual: true, online: true, outputRows: zonal.summary.outputRows, groups: summarized.summary.groups, auditStatus: verified.auditStatus }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
