import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_ENVIRONMENTAL_INDICES_PYTHON?.trim()
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
const temporary = await mkdtemp(join(tmpdir(), 'cryo-indices-host-'))
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
shape = (20, 20)
x = np.linspace(0.1, 0.8, shape[1], dtype='float32')[None, :].repeat(shape[0], axis=0)
arrays = {
    'ndvi': x,
    'lst': 15 + 20 * (1 - x) + np.linspace(0, 3, shape[0], dtype='float32')[:, None],
}
for role, values in arrays.items():
    with rasterio.open(root / f'{role}.tif', 'w', driver='GTiff', width=shape[1], height=shape[0], count=1, dtype='float32', crs='EPSG:4326', transform=rasterio.Affine(1, 0, 0, 0, -1, 0), nodata=-9999) as target:
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
  const tool = tools.get('environmental_indices')
  assert.equal(typeof tool?.execute, 'function', 'environmental_indices was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }
  const common = { projectRoot, inputs: { ndvi: 'ndvi.tif', lst: 'lst.tif' } }

  const inspected = await call({ action: 'inspect', ...common })
  assert.equal(inspected.sources.length, 2)
  const result = await call({ action: 'calculate', ...common, indices: ['fvc', 'lst', 'tvdi'], ndviBins: 5, minBinPoints: 5 })
  assert.equal(result.validPixels, 400)
  assert.equal(result.tvdiFit.pointsUsed, 400)
  assert.ok(result.tvdiFit.dryEdge.slope < 0)
  assert.ok(existsSync(result.paths.raster) && existsSync(result.paths.fit) && existsSync(result.paths.manifest))

  const invalid = await raw({ action: 'inspect', projectRoot, inputs: { ndvi: '../outside.tif' } })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'INDICES_ERROR')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    validPixels: result.validPixels,
    fitPoints: result.tvdiFit.pointsUsed,
    indices: result.indices,
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
