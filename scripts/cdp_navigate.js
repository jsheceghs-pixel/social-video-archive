// CDP 导航 helper: node cdp_navigate.js <wsUrl> <url> [waitMs]
// 导航页面并等待加载，然后返回页面状态
const WebSocket = require('ws');
const ws = new WebSocket(process.argv[2]);
const targetUrl = process.argv[3];
const waitMs = parseInt(process.argv[4] || '10000', 10);
let id = 0; const pend = new Map();
function send(m, p = {}) {
  return new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
}
ws.on('message', d => {
  const m = JSON.parse(d.toString());
  if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(m.error.message)) : res(m.result); }
});
ws.on('open', async () => {
  try {
    await send('Page.enable');
    await send('Page.navigate', { url: targetUrl });
    await new Promise(r => setTimeout(r, waitMs));
    const st = await send('Runtime.evaluate', {
      expression: `JSON.stringify({url: location.href, title: document.title.slice(0, 50), hasSsr: !!(window.__INITIAL_STATE__)})`,
      returnByValue: true
    });
    console.log(JSON.stringify({ value: st.result ? st.result.value : null }));
  } catch (e) { console.error(JSON.stringify({ error: e.message })); }
  ws.close(); process.exit(0);
});
