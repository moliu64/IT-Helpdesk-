import fs from 'node:fs';
const p = 'output/tools/e2e-rag.mjs';
let s = fs.readFileSync(p, 'utf8');
let count = 0;
function rep(old, neu, expected = 1) {
  const n = s.split(old).length - 1;
  if (n !== expected) { console.log(`MISMATCH (${n}) for: ${old.slice(0, 70).replace(/\n/g, '\\n')}`); return; }
  s = s.split(old).join(neu); count += 1;
}
// remove accidental label statement
rep(`rateLimitNothing: { /* label placeholder to keep parse simple */ }\n`, '');
// attach stable probe attribute for exact field mapping
rep(`.filter((el) => el.getClientRects().length > 0 && !el.disabled && el.type !== 'hidden')
      .map((el, index) => {
        const label = el.closest('label');`,
`.filter((el) => el.getClientRects().length > 0 && !el.disabled && el.type !== 'hidden')
      .map((el, index) => {
        el.setAttribute('data-e2e-field', String(index));
        const label = el.closest('label');`);
// add fieldLocator helper after pickField definition
rep(`function pickField(fields, patterns, tag) {
  return fields.find((field) => (!tag || field.tag === tag) && patterns.some((pattern) => pattern.test(\`\${field.labelText} \${field.placeholder} \${field.name} \${field.id}\`)));
}`,
`function pickField(fields, patterns, tag) {
  return fields.find((field) => (!tag || field.tag === tag) && patterns.some((pattern) => pattern.test(\`\${field.labelText} \${field.placeholder} \${field.name} \${field.id}\`)));
}
function fieldLocator(scope, index) {
  return scope.locator(\`[data-e2e-field="\${index}"]\`).first();
}`);
// replace nth-based field access with probe-attribute mapping
for (const [field, varName] of [['documentInput', 'document'], ['newNameInput', 'document'], ['titleField', 'title'], ['textField', 'text'], ['stepsField', 'steps.join(\'\\n\')']]) {
  // handled individually below
}
rep(`await scope.locator('input,textarea,select').nth(documentInput.index).fill(document);`, `await fieldLocator(scope, documentInput.index).fill(document);`);
rep(`await scope.locator('input,textarea,select').nth(newNameInput.index).fill(document);`, `await fieldLocator(scope, newNameInput.index).fill(document);`);
rep(`  await scope.locator('input').nth(0).count(); // keep locator API warm\n`, '');
rep(`await scope.locator('input,textarea,select').nth(titleField.index).fill(title);`, `await fieldLocator(scope, titleField.index).fill(title);`);
rep(`await scope.locator('input,textarea,select').nth(textField.index).fill(text);`, `await fieldLocator(scope, textField.index).fill(text);`);
rep(`await scope.locator('input,textarea,select').nth(stepsField.index).fill(steps.join('\\n'));`, `await fieldLocator(scope, stepsField.index).fill(steps.join('\\n'));`);
// drop unused duplicate
rep(`let initialLibrary = null;\nlet createdEntryId = null;\n`, `let initialLibrary = null;\n`);
fs.writeFileSync(p, s, 'utf8');
console.log('replacements:', count);
