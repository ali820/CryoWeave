import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_MODEL_EXPERIMENTS_PYTHON?.trim()
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

const temporary = await mkdtemp(join(tmpdir(), 'cryo-model-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  const rows = ['x,group,label,order']
  for (let index = 0; index < 40; index += 1) {
    const x = index % 10
    rows.push([x, Math.floor(index / 10), x >= 5 ? 'yes' : 'no', index].join(','))
  }
  await writeFile(join(projectRoot, 'data.csv'), `${rows.join('\n')}\n`, 'utf8')

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
  const tool = tools.get('model_experiments')
  assert.equal(typeof tool?.execute, 'function', 'model_experiments was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const common = {
    projectRoot,
    table: 'data.csv',
    featureColumns: ['x'],
    targetColumn: 'label',
    task: 'classification',
    positiveLabel: 'yes',
    groupColumn: 'group',
    model: 'logistic',
    cvFolds: 2,
    randomState: 42,
  }
  const evaluated = await call({ action: 'evaluate', ...common })
  assert.equal(evaluated.summary.rows, 40)
  assert.equal(evaluated.summary.foldCount, 2)
  assert.ok(evaluated.summary.metrics.accuracy >= 0.9)
  assert.equal(evaluated.summary.leakageChecks.groupOverlapCount, 0)
  assert.equal(evaluated.summary.leakageChecks.targetExcludedFromFeatures, true)
  assert.ok(existsSync(evaluated.paths.predictions) && existsSync(evaluated.paths.summary))

  const important = await call({ action: 'importance', ...common, nRepeats: 3 })
  assert.equal(important.summary.notShap, true)
  assert.equal(important.summary.method, 'permutation_importance')
  assert.equal(important.summary.features.length, 1)
  assert.equal(important.summary.features[0].feature, 'x')

  const invalid = await raw({ action: 'evaluate', ...common, table: '../outside.csv' })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'MODEL_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    rows: evaluated.summary.rows,
    folds: evaluated.summary.foldCount,
    accuracy: evaluated.summary.metrics.accuracy,
    importanceMethod: important.summary.method,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
