import fs from 'node:fs';
const p = 'output/tools/e2e-rag.mjs';
let s = fs.readFileSync(p, 'utf8');
let count = 0;
function rep(old, neu) {
  if (!s.includes(old)) { console.log('MISS:', old.slice(0, 80).replace(/\n/g, '\\n')); return; }
  s = s.split(old).join(neu); count += 1;
}
// 1) new-entry: prefer the explicit __new__ option over docs whose title contains 新建
rep(`      const newOption = options.find((option) => /新建|新增|自定义|__new|其他/.test(\`\${option.text} \${option.value}\`)) || options[options.length - 1];`,
`      const newOption = options.find((option) => option.value === '__new__')
        || options.find((option) => /新建文档|＋ 新建|新建…/.test(option.text))
        || options[options.length - 1];`);
// 2) move: explicit confirm button selector (avoid re-clicking [data-move])
rep(`      const confirm = await firstVisible(scope, ['button:has-text("移动")', 'button:has-text("确定")', 'button:has-text("保存")', 'button[type="submit"]']);`,
`      const confirm = await firstVisible(scope, ['[data-move-confirm]', 'button:has-text("确认移动")', 'button:has-text("确认")', 'button:has-text("确定")', 'button[type="submit"]']);`);
rep(`  const confirm = await firstVisible(scope, ['button:has-text("移动")', 'button:has-text("确定")', 'button:has-text("保存")']);`,
`  const confirm = await firstVisible(scope, ['[data-move-confirm]', 'button:has-text("确认移动")', 'button:has-text("确认")', 'button:has-text("确定")']);`);
// 3) avoid unhandled rejection from the response race
rep(`    const first = await Promise.race([\n      responsePromise.then((response) => ({ response })),\n      new Promise((resolve) => setTimeout(() => resolve(null), 1500)),\n    ]);`,
`    const first = await Promise.race([\n      responsePromise.then((response) => ({ response })).catch(() => null),\n      new Promise((resolve) => setTimeout(() => resolve(null), 1500)),\n    ]);`);
// 4) new entry form: after selecting __new__, the unveiled input is #newEntryDocumentName ("新文档名")
rep(`      fields = await describeFields(scope);\n      const newNameInput = pickField(fields.filter((field) => !usedIndexes.has(field.index)), [/新文档/, /新建/, /新名字/, /文档名/], 'INPUT')\n        || fields.filter((field) => field.tag === 'INPUT' && !usedIndexes.has(field.index))[0];`,
`      fields = await describeFields(scope);\n      const newNameInput = pickField(fields.filter((field) => !usedIndexes.has(field.index)), [/新文档/, /新建/, /新名字/, /文档名/], 'INPUT')\n        || fields.find((field) => field.id === 'newEntryDocumentName')\n        || fields.filter((field) => field.tag === 'INPUT' && !usedIndexes.has(field.index))[0];`);
// 5) helper: wait until a document row is rendered
rep(`async function waitEntry(page, entryId, timeout = 20000) {`,
`async function waitDocRow(page, document, timeout = 30000) {
  await waitUntil(() => page.evaluate((name) => [...document.querySelectorAll('#docList .doc-item')]
    .some((item) => item.dataset.document === name), document), timeout, 300, \`document row \${document}\`);
}
async function waitEntry(page, entryId, timeout = 20000) {`);
// 6) r5a: wait for the new manual doc row before clicking it
rep(`    await page.waitForTimeout(800);\n    await waitLibraryRendered(page);\n    await clickDoc(page, MANUAL_DOC);\n    await waitEntry(page, state.createdEntryId);`,
`    await page.waitForTimeout(800);\n    await waitLibraryRendered(page);\n    await waitDocRow(page, MANUAL_DOC);\n    await clickDoc(page, MANUAL_DOC);\n    await waitEntry(page, state.createdEntryId);`);
// 7) r5b/r5c: same guard when selecting the manual doc
rep(`    await clickDoc(page, MANUAL_DOC);\n    const card = await waitEntry(page, state.createdEntryId);`,
`    await waitDocRow(page, MANUAL_DOC);\n    await clickDoc(page, MANUAL_DOC);\n    const card = await waitEntry(page, state.createdEntryId);`);
fs.writeFileSync(p, s, 'utf8');
console.log('replacements:', count);
