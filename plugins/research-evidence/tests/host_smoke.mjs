import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../index.js'

function selectPython() {
  const configured = process.env.CRYO_PYTHON?.trim() || process.env.CRYO_RESEARCH_EVIDENCE_PYTHON?.trim()
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

const temporary = await mkdtemp(join(tmpdir(), 'cryo-evidence-host-'))
try {
  const projectRoot = join(temporary, 'project')
  await mkdir(join(projectRoot, 'figures'), { recursive: true })
  const dataPath = join(projectRoot, 'data.csv')
  const figurePath = join(projectRoot, 'figures', 'figure.png')
  await writeFile(dataPath, 'a,b\n1,2\n', 'utf8')
  await writeFile(figurePath, 'png', 'utf8')
  await writeFile(join(projectRoot, 'CURRENT_AUTHORITY.md'), '# Demo\n更新日期：2026-01-01\n', 'utf8')
  const digest = async (path) => createHash('sha256').update(await readFile(path)).digest('hex')
  await writeFile(join(projectRoot, 'figure.export.json'), JSON.stringify({
    source_data: [{ path: 'data.csv', sha256: await digest(dataPath) }],
    cache_assets: [],
    outputs: { png: { path: 'figures/figure.png', bytes: 3, sha256: await digest(figurePath) } },
  }), 'utf8')
  await writeFile(join(projectRoot, 'claim_evidence_map.csv'), 'claim_id,proposed_claim,evidence_source_ids,evidence_status,gate\nC1,Claim,S1,PASS,OPEN\n', 'utf8')
  await writeFile(join(projectRoot, 'numeric_conflict_register.csv'), 'conflict_id,metric,canonical_value,other_value,status\nN1,m,1,2,resolved\n', 'utf8')

  const tools = new Map()
  const ctx = {
    effect: (callback) => callback(),
    tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
  }
  apply(ctx, {
    pythonPath: selectPython(),
    allowedRoots: [projectRoot],
    timeoutMs: 60000,
    maxHashBytes: 1024 * 1024,
  })
  const tool = tools.get('research_evidence_audit')
  assert.equal(typeof tool?.execute, 'function', 'research_evidence_audit was not registered')
  const raw = async (args) => JSON.parse(await tool.execute(args, { signal: AbortSignal.timeout(60000) }))
  const call = async (args) => {
    const result = await raw(args)
    assert.equal(result.ok, true, JSON.stringify(result))
    return result
  }

  const clean = await call({ projectRoot })
  assert.equal(clean.auditStatus, 'clean')
  assert.equal(clean.details.authority.title, 'Demo')
  assert.equal(clean.details.figures.verifiedReferences, 2)
  assert.equal(clean.details.claims.claims, 1)
  assert.equal(clean.details.conflicts.unresolved, 0)

  await writeFile(dataPath, 'a,b\n9,9\n', 'utf8')
  const changed = await call({ projectRoot, checks: ['figures'] })
  assert.equal(changed.auditStatus, 'issues_found')
  assert.ok(changed.findings.some((finding) => finding.code === 'SHA256_MISMATCH'))

  const invalid = await raw({ projectRoot, maxFiles: -1 })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.error?.code, 'EVIDENCE_INVALID_REQUEST')

  console.log(JSON.stringify({
    ok: true,
    offline: true,
    standardLibraryWorker: true,
    verifiedReferences: clean.details.figures.verifiedReferences,
    staleDetection: 'SHA256_MISMATCH',
    errorProtocol: invalid.error.code,
  }))
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
}
