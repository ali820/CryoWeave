import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_INSAR_OPS_PYTHON?.trim()
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

const pythonPath = selectPython()
const temporary = await mkdtemp(join(tmpdir(), 'cryo-insar-host-'))
try {
  const projectRoot = join(temporary, 'project')
  const stateRoot = join(temporary, 'state')
  await mkdir(projectRoot, { recursive: true })
  const fixture = spawnSync(pythonPath, ['-c', `
import sys
import numpy as np
import rasterio
from pathlib import Path
root = Path(sys.argv[1])
velocity = np.zeros((20, 20), dtype='float32')
coherence = np.full((20, 20), 0.9, dtype='float32')
velocity[2:6, 2:6] = 20
velocity[12:16, 12:16] = -15
for name, values in [('velocity', velocity), ('coherence', coherence)]:
    with rasterio.open(root / f'{name}.tif', 'w', driver='GTiff', width=20, height=20, count=1, dtype='float32', crs='EPSG:32645', transform=rasterio.Affine(100, 0, 0, 0, -100, 0), nodata=-9999) as target:
        target.write(values, 1)
`, projectRoot], { encoding: 'utf8', timeout: 60000 })
  assert.equal(fixture.status, 0, fixture.stderr || fixture.error?.message)

  const tools = new Map()
  const ctx = {
    effect: (callback) => callback(),
    tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
  }
  apply(ctx, {
    pythonPath,
    stateRoot,
    allowedRoots: [projectRoot],
    timeoutMs: 60000,
    maxHashBytes: 1024 * 1024,
  })
  const tool = tools.get('insar_ops')
  assert.equal(typeof tool?.execute, 'function', 'insar_ops was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const result = await call({
    action: 'analyze', projectRoot,
    velocity: 'velocity.tif', coherence: 'coherence.tif',
    velocityThreshold: 10, coherenceThreshold: 0.7, minAreaPixels: 4, maxClusters: 10,
  })
  assert.equal(result.summary.validPixels, 32)
  assert.equal(result.summary.clusterCount, 2)
  assert.deepEqual(result.rows.map((row) => row.meanVelocity).sort((a, b) => a - b), [-15, 20])
  assert.ok(existsSync(result.paths.clusters) && existsSync(result.paths.summary) && existsSync(result.paths.manifest))

  const invalid = await raw({
    action: 'analyze', projectRoot,
    velocity: '../outside.tif', coherence: 'coherence.tif',
  })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'INSAR_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    validPixels: result.summary.validPixels,
    clusterCount: result.summary.clusterCount,
    totalAnomalyAreaKm2: result.summary.totalAnomalyAreaKm2,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
