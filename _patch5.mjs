import fs from 'node:fs';
const p = 'output/tools/e2e-rag.mjs';
let s = fs.readFileSync(p, 'utf8');
let count = 0;
function rep(old, neu) {
  if (!s.includes(old)) { console.log('MISS:', old.slice(0, 90).replace(/\n/g, '\\n')); return; }
  s = s.split(old).join(neu); count += 1;
}
// 1) strip PowerShell BOM from env json
rep(`const ENV = (() => {
  try { return JSON.parse(fs.readFileSync(ENV_FILE, 'utf8')); } catch { return null; }
})();`,
`const ENV = (() => {
  try {
    const raw = fs.readFileSync(ENV_FILE, 'utf8').replace(/^\\uFEFF/, '');
    return JSON.parse(raw);
  } catch { return null; }
})();`);
// 2) r5c: toggle may only dirty the form; accept explicit save fallback
rep(`    const updatePromise = waitForResponse(page, isResponse('/api/rag/entries/update'), 30000).catch(() => null);
    await toggle.click();
    let updateResponse = await updatePromise;
    if (!updateResponse) {
      const save = await saveEntryCard(page, card);
      evidence.r5c = { saveStatusFallback: save.status };
    }`,
`    const updatePromise = waitForResponse(page, isResponse('/api/rag/entries/update'), 3000).catch(() => null);
    await toggle.click();
    let updateResponse = await updatePromise;
    let saveStatus = null;
    if (!updateResponse) {
      // The switch only marks the card dirty on this page; the 保存 button persists it.
      const save = await saveEntryCard(page, card);
      saveStatus = save.status;
    }
    evidence.r5c = { saveStatusFallback: saveStatus };`);
rep(`    assert(updateResponse && updateResponse.status() === 200, \`disable update -> HTTP \${updateResponse ? updateResponse.status() : 'n/a'}\`);`,
`    assert((updateResponse && updateResponse.status() === 200) || saveStatus === 200,
      \`disable save -> update=\${updateResponse ? updateResponse.status() : 'n/a'} save=\${saveStatus}\`);`);
// 3) upload fixtures: keep the ORIGINAL filename in a dedicated temp dir
rep(`    const fileName = \`e2e-upload-\${RUN_STAMP}.md\`;
    const filePath = path.join(REPO_ROOT, 'output', 'tools', \`_\${fileName}\`);`,
`    const fileName = \`e2e-upload-\${RUN_STAMP}.md\`;
    const uploadDir = path.join(REPO_ROOT, 'output', 'tools', 'rag-e2e-uploads');
    fs.mkdirSync(uploadDir, { recursive: true });
    const filePath = path.join(uploadDir, fileName);`);
rep(`    const emptyName = \`e2e-empty-\${RUN_STAMP}.md\`;
    const emptyPath = path.join(REPO_ROOT, 'output', 'tools', \`_\${emptyName}\`);`,
`    const emptyName = \`e2e-empty-\${RUN_STAMP}.md\`;
    const uploadDir = path.join(REPO_ROOT, 'output', 'tools', 'rag-e2e-uploads');
    fs.mkdirSync(uploadDir, { recursive: true });
    const emptyPath = path.join(uploadDir, emptyName);`);
// 4) empty file message: server rejects blank content before parsing ("上传文件不能为空")
rep(`    assert(body && /未能.*文本|扫描版|提取/.test(String(body.error || '')), \`400 error message not friendly: \${short(body)}\`);`,
`    assert(body && /未能.*文本|扫描版|提取|不能为空|为空/.test(String(body.error || '')), \`400 error message not friendly: \${short(body)}\`);`);
rep(`        .filter((item) => item.visible && /未能|提取|扫描|失败|错误/.test(item.text));`,
`        .filter((item) => item.visible && /未能|提取|扫描|失败|错误|为空/.test(item.text));`);
// 5) deleteDocument: wait for the row to be rendered instead of assuming it is there
rep(`async function deleteDocument(page, document) {
  await page.evaluate((name) => {`,
`async function deleteDocument(page, document) {
  await waitDocRow(page, document, 30000);
  await page.evaluate((name) => {`);
// 6) r5f: explicit wait for the freshly created doc row after reload
rep(`    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    await waitLibraryRendered(page);
    const del = await deleteDocument(page, DELETE_DOC);`,
`    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    await waitLibraryRendered(page);
    await waitDocRow(page, DELETE_DOC, 30000);
    const del = await deleteDocument(page, DELETE_DOC);`);
fs.writeFileSync(p, s, 'utf8');
console.log('replacements:', count);
