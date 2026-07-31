// CDP eval helper v2: node cdp_eval.js <wsUrl> <expressionFile>
const WebSocket = require('ws');
const fs = require('fs');
const ws = new WebSocket(process.argv[2]);
const expr = fs.readFileSync(process.argv[3], 'utf8');
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
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    console.log(JSON.stringify({ value: r.result ? r.result.value : null }));
  } catch (e) { console.error(JSON.stringify({ error: e.message })); }
  ws.close(); process.exit(0);
});
