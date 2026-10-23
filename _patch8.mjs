import fs from 'node:fs';
const p = 'output/tools/test-server.ps1';
let s = fs.readFileSync(p, 'utf8');
const old = `$dbPath = Join-Path $toolsDir 'test-helpdesk.db'
$ragStorePath = Join-Path $toolsDir 'rag-e2e-store.json'`;
const neu = `$dbPath = Join-Path $toolsDir 'test-helpdesk.db'
$ragDbPath = Join-Path $toolsDir 'rag-e2e.db'
$ragStorePath = Join-Path $toolsDir 'rag-e2e-store.json'`;
if (!s.includes(old)) { console.log('MISS db path block'); process.exit(1); }
s = s.replace(old, neu);
const old2 = `        if ($FreshDb -and (Test-Path $dbPath)) {`;
const neu2 = `        if ($RagIsolation) { $dbPath = $ragDbPath }
        if ($FreshDb -and (Test-Path $dbPath)) {`;
if (!s.includes(old2)) { console.log('MISS freshdb block'); process.exit(1); }
s = s.replace(old2, neu2);
fs.writeFileSync(p, s, 'utf8');
console.log('patched test-server.ps1 (RAG isolation db = rag-e2e.db)');
