import fs from 'node:fs';
const p = 'output/tools/e2e-rag.mjs';
let s = fs.readFileSync(p, 'utf8');
let count = 0;
function rep(old, neu) { if (!s.includes(old)) { console.log('MISS:', old.slice(0, 100).replace(/\n/g,'\\n')); return; } s = s.split(old).join(neu); count += 1; }
// 1) deleteDocument: find+click in a single evaluate (no attribute race with re-renders)
const oldDeleteDoc = `async function deleteDocument(page, document) {
  await waitDocRow(page, document, 30000);
  await page.evaluate((name) => {
    const row = [...document.querySelectorAll('#docList .doc-item')].find((item) => item.dataset.document === name);
    if (row) row.setAttribute('data-verify-doc-row', '1');
  });
  const row = page.locator('[data-verify-doc-row="1"]');
  assert((await row.count()) > 0, \`document row not found: \${document}\`);
  await row.hover().catch(() => {});
  await page.waitForTimeout(300);
  page.once('dialog', (dialog) => dialog.accept().catch(() => {}));
  const responsePromise = waitForResponse(page, isResponse('/api/rag/documents/delete'), 60000).catch(() => null);
  const clicked = await page.evaluate(() => {
    const row = document.querySelector('[data-verify-doc-row="1"]');
    const selectors = 'button[aria-label*="删除"], button[title*="删除"], [data-delete-document], [data-delete], .doc-delete, .delete';
    const visible = (el) => el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    let button = [...row.querySelectorAll(selectors)].find(visible);
    if (!button) button = [...row.querySelectorAll('button')].find((el) => /删除|移除/.test(el.textContent || '') && visible(el));
    if (!button) return null;
    button.click();
    return (button.getAttribute('aria-label') || button.getAttribute('title') || button.textContent || '').trim().slice(0, 40);
  });
  assert(clicked, \`no delete-document control found in row \${document}\`);`;
rep(oldDeleteDoc, `async function deleteDocument(page, document) {
  page.once('dialog', (dialog) => dialog.accept().catch(() => {}));
  const responsePromise = waitForResponse(page, isResponse('/api/rag/documents/delete'), 60000).catch(() => null);
  // Single evaluate: locate the row + click its delete control atomically (list may re-render).
  let clicked = null;
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline && !clicked) {
    clicked = await page.evaluate((name) => {
      const row = [...document.querySelectorAll('#docList .doc-item')].find((item) => item.dataset.document === name);
      if (!row) return null;
      const selectors = 'button[aria-label*="删除"], button[title*="删除"], [data-delete-doc], [data-delete-document], [data-delete], .doc-delete, .delete';
      const visible = (el) => el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
      let button = [...row.querySelectorAll(selectors)].find(visible);
      if (!button) button = [...row.querySelectorAll('button')].find((el) => /删除|移除/.test(el.textContent || '') && visible(el));
      if (!button) return null;
      button.click();
      return (button.getAttribute('aria-label') || button.getAttribute('title') || button.textContent || '').trim().slice(0, 40);
    }, document);
    if (!clicked) await page.waitForTimeout(400);
  }
  assert(clicked, \`no delete-document control found in row \${document}\`);`);
// 2) r6b: old-content check via managed entries (search matches don't expose chunk text)
rep(`    const search1 = await ragSearch(page, \`E2E-UPLOAD-\${RUN_STAMP}\`);
    const hit1 = entryScore(search1.body && search1.body.matches, { document: state.uploadName });
    evidence.r6b = { status: response.status(), body, toast, ids, duplicateIds: ids.length !== uniqueIds.size, hit2, hit1 };`,
`    const oldToken = \`E2E-UPLOAD-\${RUN_STAMP}\`;
    const entriesText = list.map((entry) => \`\${entry.title || ''} \${entry.text || ''} \${(entry.steps || []).join(' ')}\`).join('\\n');
    const oldContentGone = !entriesText.includes(oldToken);
    const newContentStored = entriesText.includes(\`E2E-UPLOAD2-\${RUN_STAMP}\`);
    evidence.r6b = { status: response.status(), body, toast, ids, duplicateIds: ids.length !== uniqueIds.size, hit2, oldContentGone, newContentStored };`);
rep(`    assert(hit2.hit, \`new content not searchable after re-upload (matches=\${short(hit2.sources)})\`);
    assert(!hit1.hit, \`old content still searchable after same-name replace: \${short(hit1.hit)}\`);`,
`    assert(hit2.hit, \`new content not searchable after re-upload (matches=\${short(hit2.sources)})\`);
    assert(newContentStored, 'new content not present in the managed entries after re-upload');
    assert(oldContentGone, 'old content still present in the managed entries after same-name replace');`);
// 3) r6c inline error: page renders it in #toolbarMsg.toolbar-msg
rep(`      const candidates = [...document.querySelectorAll('[role="alert"], .error, .form-error, #message, #toast, .toast')];`,
`      const candidates = [...document.querySelectorAll('[role="alert"], .error, .form-error, #message, #toast, .toast, #toolbarMsg, .toolbar-msg, [class*="msg"]')];`);
fs.writeFileSync(p, s, 'utf8');
console.log('replacements:', count);
