import fs from 'node:fs';
const p = 'output/tools/e2e-userpage.mjs';
let s = fs.readFileSync(p, 'utf8');
let changed = 0;
function rep(old, neu) { if (!s.includes(old)) { console.log('MISS:', old.slice(0,60)); return; } s = s.replace(old, neu); changed += 1; }
// u1 evidence: record desktop sidebar-close visibility (visual defect detector)
rep(
`    const markers = await mainPage.evaluate(() => ({`,
`    const markers = await mainPage.evaluate(() => ({`);
rep(
`      identity: Boolean(document.querySelector('#identity')),
    }));`,
`      identity: Boolean(document.querySelector('#identity')),
    }));
    const desktopSideClose = await mainPage.evaluate(() => {
      const el = document.querySelector('#sidebarClose');
      if (!el) return null;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return { display: style.display, visibility: style.visibility, width: Math.round(rect.width), height: Math.round(rect.height), innerWidth: window.innerWidth };
    });`);
rep(
`      navigationStatus: navigation ? navigation.status() : null, title, markers, challenges: localChallenges,
    };`,
`      navigationStatus: navigation ? navigation.status() : null, title, markers, desktopSideClose, challenges: localChallenges,
    };`);
// u2 settle before screenshot
rep(
`    const bubbleCount = await mainPage.locator('.message').count();
    const screenshot = SHOT('userpage-empty-1280.png');
    await mainPage.screenshot({ path: screenshot });`,
`    const bubbleCount = await mainPage.locator('.message').count();
    await mainPage.waitForTimeout(600); // let msg-in / fade animations settle
    const screenshot = SHOT('userpage-empty-1280.png');
    await mainPage.screenshot({ path: screenshot });`);
// u3 settle before screenshot
rep(
`    const screenshot = SHOT('userpage-chat-1280.png');
    await page.screenshot({ path: screenshot });
    evidence.u3 = {`,
`    await page.waitForTimeout(700); // msg-in is 0.18s; never capture mid-animation
    const screenshot = SHOT('userpage-chat-1280.png');
    await page.screenshot({ path: screenshot });
    evidence.u3 = {`);
// u6 settle before screenshot
rep(
`    }, 30000, 400, 'ticket success card after modal close');
    const screenshot = SHOT('userpage-ticket-1280.png');
    await page.screenshot({ path: screenshot });`,
`    }, 30000, 400, 'ticket success card after modal close');
    await page.waitForTimeout(700); // settle the ticket-card msg-in animation
    const screenshot = SHOT('userpage-ticket-1280.png');
    await page.screenshot({ path: screenshot });`);
// u8 drawer settle a bit longer
rep(
`    await page.waitForTimeout(700);
    const opened = await measure();`,
`    await page.waitForTimeout(900);
    const opened = await measure();`);
// u2 detail: warn if desktop close icon visible (cosmetic)
rep(
`    return \`sidebar+welcome+composer visible, \${chips.count} chips (\${chips.matched.length}/4 spec texts), shot=\${rel(screenshot)}\`;`,
`    const closeVisible = evidence.u1 && evidence.u1.desktopSideClose && evidence.u1.desktopSideClose.display !== 'none' && evidence.u1.desktopSideClose.width > 0;
    return \`sidebar+welcome+composer visible, \${chips.count} chips (\${chips.matched.length}/4 spec texts), shot=\${rel(screenshot)}\${closeVisible ? '; WARN desktop #sidebarClose is visible (CSS bug)' : ''}\`;`);
fs.writeFileSync(p, s, 'utf8');
console.log('edits applied:', changed);
