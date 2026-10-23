import fs from 'node:fs';
const p = 'output/tools/e2e.mjs';
let s = fs.readFileSync(p, 'utf8');
const old = `function short(value, max = 400) {\n  const text = typeof value === 'string' ? value : JSON.stringify(value);\n  return text.length > max ? \`\${text.slice(0, max)}…\` : text;\n}`;
const neu = `function short(value, max = 400) {\n  let text;\n  if (typeof value === 'string') text = value;\n  else if (value === undefined) text = 'undefined';\n  else if (value === null) text = 'null';\n  else {\n    try { text = JSON.stringify(value); } catch { text = String(value); }\n  }\n  text = String(text);\n  return text.length > max ? \`\${text.slice(0, max)}…\` : text;\n}`;
if (!s.includes(old)) { console.log('PATTERN NOT FOUND'); process.exit(1); }
fs.writeFileSync(p, s.replace(old, neu), 'utf8');
console.log('patched e2e.mjs short()');
