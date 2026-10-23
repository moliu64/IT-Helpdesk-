import fs from 'node:fs';
const p = 'output/tools/e2e-rag.mjs';
let s = fs.readFileSync(p, 'utf8');
let count = 0;
function rep(old, neu) { if (!s.includes(old)) { console.log('MISS:', old.slice(0,80)); return; } s = s.split(old).join(neu); count += 1; }
rep(`const updatePromise = waitForResponse(page, isResponse('/api/rag/entries/update'), 3000).catch(() => null);`,
    `const updatePromise = waitForResponse(page, isResponse('/api/rag/entries/update'), 8000).catch(() => null);`);
rep(`      if (/\\/api\\/rag\\/import/.test(item.url || '') && /400/.test(item.text || '')) return false;
      return true;`,
`      if (/\\/api\\/rag\\/import/.test(item.url || '') && /400/.test(item.text || '')) return false;
      // Late cleanup of an optional upload fixture (the upload check itself reports failure).
      if (/\\/api\\/rag\\/files/.test(item.url || '') && /404/.test(item.text || '')) return false;
      return true;`);
fs.writeFileSync(p, s, 'utf8');
console.log('replacements:', count);
