import { existsSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const requested = new Set(process.argv.slice(2))
const plugins = readdirSync(join(root, 'plugins'), { withFileTypes: true })
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .filter((name) => !requested.size || requested.has(name))
  .sort()

if (!plugins.length || [...requested].some((name) => !plugins.includes(name))) {
  throw new Error(`Unknown plugin selection: ${[...requested].join(', ') || 'none'}`)
}

function run(args, options = {}) {
  const result = spawnSync(process.execPath, args, {
    cwd: root,
    stdio: 'inherit',
    timeout: 180000,
    ...options,
  })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`Command failed (${result.status ?? 'timeout'}): ${args.join(' ')}`)
}

function pythonFor(plugin) {
  if (process.env.CRYO_PYTHON) return process.env.CRYO_PYTHON
  const directory = join(root, 'plugins', plugin, '.venv')
  const candidates = [join(directory, 'Scripts', 'python.exe'), join(directory, 'bin', 'python')]
  return candidates.find((path) => existsSync(path)) ?? 'python'
}

let hostTests = 0
for (const plugin of plugins) {
  const entry = join(root, 'plugins', plugin, 'index.js')
  console.log(`=== Host syntax: ${plugin} ===`)
  run(['--check', entry])
  const test = join(root, 'plugins', plugin, 'tests', 'host_smoke.mjs')
  if (!existsSync(test)) {
    console.log(`No Host smoke test: ${plugin}`)
    continue
  }
  const python = pythonFor(plugin)
  console.log(`=== Host smoke: ${plugin} (${python}) ===`)
  run([test], { env: { ...process.env, CRYO_PYTHON: python } })
  hostTests += 1
}
console.log(`Host validation passed: ${plugins.length} syntax checks, ${hostTests} smoke tests.`)
