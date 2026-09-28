const fs = require('fs');
const vm = require('vm');
const s = fs.readFileSync('page.html', 'utf8');
const m = s.match(/<script>([\s\S]*)<\/script>/);
const code = m[1];
try {
  new vm.Script(code);
  console.log('语法OK');
} catch (e) {
  console.log('语法错误:', e.message);
  const lines = code.split('\n');
  // 从堆栈中提取行号
  const sm = e.stack.match(/<anonymous>:(\d+)/);
  if (sm) {
    const ln = parseInt(sm[1], 10) - 1; // vm.Script 偏移
    console.log('出错行号(脚本内):', ln);
    for (let i = Math.max(0, ln - 3); i < Math.min(lines.length, ln + 2); i++) {
      console.log((i === ln ? '>>' : '  '), i + 1, ':', lines[i].slice(0, 160));
    }
  }
}
