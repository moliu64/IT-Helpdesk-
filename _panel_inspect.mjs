import fs from 'node:fs';
const s = fs.readFileSync('项目控制面板.ps1','utf8');
for (const kw of ['Mutex','一键启动项目','停止项目','运行中','已停止','FormClosing','FormClosed','Text =','FindWindow','SetForegroundWindow','启动后可关闭','Title']) {
  const idx = s.indexOf(kw);
  console.log('====', kw, 'idx', idx);
  if (idx >= 0) console.log(s.slice(Math.max(0,idx-220), idx+320).replace(/\r/g,''));
}
