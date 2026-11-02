import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_FIELD_MULTISOURCE_PYTHON?.trim()
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

const temporary = await mkdtemp(join(tmpdir(), 'cryo-field-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  await writeFile(join(projectRoot, 'a.csv'), 'date,temp\n2024-01-01,1\n2024-01-02,2\n2024-01-03,3\n', 'utf8')
  await writeFile(join(projectRoot, 'b.csv'), 'date,temp\n2024-01-01,2\n2024-01-02,3\n2024-01-03,4\n', 'utf8')

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
  const tool = tools.get('field_multisource')
  assert.equal(typeof tool?.execute, 'function', 'field_multisource was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const inspected = await call({ action: 'inspect', projectRoot, source: 'a.csv', timeColumn: 'date', valueColumns: ['temp'] })
  assert.equal(inspected.summary.rows, 3)
  assert.equal(inspected.summary.validTimes, 3)
  const quality = await call({ action: 'qc', projectRoot, source: 'a.csv', timeColumn: 'date', valueColumns: ['temp'], minValue: -10, maxValue: 10, maxGapHours: 48 })
  assert.equal(quality.summary.qaStatus, 'clean')
  const aligned = await call({
    action: 'align', projectRoot, sourceA: 'a.csv', sourceB: 'b.csv',
    timeColumnA: 'date', timeColumnB: 'date', valueColumnA: 'temp', valueColumnB: 'temp',
    maxTimeDiffSeconds: 0, maxPairs: 100,
  })
  assert.equal(aligned.summary.matchedPairs, 3)
  const compared = await call({ action: 'compare', projectRoot, table: aligned.statePaths.pairs, columnA: 'valueA', columnB: 'valueB' })
  assert.equal(compared.summary.n, 3)
  assert.ok(Math.abs(compared.summary.rmse - 1) < 1e-12)
  assert.equal(compared.summary.bias, -1)

  const invalid = await raw({ action: 'inspect', projectRoot, source: '../outside.csv' })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'FIELD_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    rows: inspected.summary.rows,
    matchedPairs: aligned.summary.matchedPairs,
    rmse: compared.summary.rmse,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
