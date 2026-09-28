const fs = require('fs');
const vm = require('vm');
const s = fs.readFileSync('page.html', 'utf8');
const m = s.match(/<script>([\s\S]*)<\/script>/);
const code = m[1];

function el(id) {
  return { id, value: '', textContent: '', innerHTML: '', style: {}, addEventListener() {} };
}
const elements = {};
const driver = `
;DATA=[
 {city:'广州市',store:'门店A',title:"14'' BIKE 900 - 青玉色",quality:'S',price:709.9,retail:999.9,sku:'4311500@S',image:''},
 {city:'西安市',store:'门店B',title:"14'' BIKE 500 - 独角兽款",quality:'B',price:249.9,retail:499.9,sku:'X1',image:''}
];TS=Date.now()/1000;
try{ render(); console.log('render() 完成, tbody 长度:', document.getElementById('tb').innerHTML.length, '| 状态栏:', document.getElementById('status').textContent.slice(0,30)); }
catch(e){ console.log('render() 错误:', e.stack.split('\\n').slice(0,4).join(' | ')); }
`;
const sandbox = {
  document: {
    getElementById(id) { if (!elements[id]) elements[id] = el(id); return elements[id]; },
    title: '', addEventListener() {},
  },
  localStorage: { getItem: () => null, setItem: () => {} },
  fetch: () => Promise.reject(new Error('mock')),
  alert: () => {}, confirm: () => true, prompt: () => null,
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, // setTimeout 必须 mock：load 失败重试链会让真实定时器挂住进程
  console, Date, JSON, Math,
};
sandbox.window = sandbox; sandbox.globalThis = sandbox;
sandbox.AudioContext = function () { this.createOscillator = () => ({ connect: () => {}, start: () => {}, stop: () => {} }); this.createGain = () => ({ connect: () => {} }); this.destination = null; };
try {
  vm.createContext(sandbox);
  vm.runInContext(code + driver, sandbox);
} catch (e) {
  console.log('执行错误:', e.stack.split('\n').slice(0, 4).join(' | '));
}
// 再测旧版 localStorage 数据（数组格式规则）
const sandbox2 = { ...sandbox, localStorage: { store: { dt_targets: JSON.stringify([["14寸|14''", '900', '青玉']]) }, getItem(k) { return this.store[k] || null; }, setItem() {} } };
sandbox2.document = sandbox.document;
sandbox2.window = sandbox2; sandbox2.globalThis = sandbox2;
try {
  vm.createContext(sandbox2);
  vm.runInContext(code + driver, sandbox2);
} catch (e) {
  console.log('旧格式执行错误:', e.stack.split('\n').slice(0, 4).join(' | '));
}
