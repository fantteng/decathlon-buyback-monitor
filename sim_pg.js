/* 一次性验证：真实 1700 辆数据下的分页/签名跳过/翻页行为 */
const fs = require('fs');
const vm = require('vm');
const http = require('http');
function get(path){return new Promise((res,rej)=>{http.get({host:'127.0.0.1',port:8787,path,headers:{'X-Dbk':'1'}},r=>{let b='';r.on('data',c=>b+=c);r.on('end',()=>res(b))}).on('error',rej)})}
function el(id){return {id,value:'',textContent:'',innerHTML:'',style:{},disabled:false,addEventListener(){}}}
(async()=>{
 const items=JSON.parse(await get('/api/query')).items;
 const page=await get('/');
 const code=page.match(/<script>([\s\S]*)<\/script>/)[1];
 const elements={};
 const sandbox={
  document:{getElementById(id){if(!elements[id])elements[id]=el(id);return elements[id]},title:'',addEventListener(){}},
  localStorage:{getItem:()=>null,setItem:()=>{}},
  fetch:()=>Promise.reject(new Error('mock')),
  alert:()=>{},confirm:()=>true,prompt:()=>null,
  setInterval:()=>0,clearInterval:()=>{},setTimeout:()=>0,
  console,Date,JSON,Math,
 };
 sandbox.window=sandbox;sandbox.globalThis=sandbox;
 vm.createContext(sandbox);
 vm.runInContext(code,sandbox);
 const nRows=h=>(h.match(/<tr/g)||[]).length;
 // 注入真实数据
 vm.runInContext(`DATA=${JSON.stringify(items)};TS=${Date.now()/1000};FAILN=0;render();`,sandbox);
 const tb=()=>sandbox.document.getElementById('tb').innerHTML;
 const pg=()=>sandbox.document.getElementById('pgInfo').textContent;
 console.log('1) 首次渲染: DOM行数 =',nRows(tb()),'(应=101内含空行头? 实际只算tbody, 应≤101) | 页码信息:',pg());
 const snap1=tb();
 // 同数据再渲染 → 应跳过重建
 vm.runInContext('render();',sandbox);
 console.log('2) 同数据二次渲染(签名跳过): DOM未变 =',tb()===snap1?'✅':'❌');
 // 翻页
 vm.runInContext('pgGo(1);',sandbox);
 console.log('3) 翻到第2页: 行数 =',nRows(tb()),'| 页码:',pg(),'| 内容确实变了 =',tb()!==snap1?'✅':'❌');
 // 翻回
 vm.runInContext('pgGo(-1);pgGo(-1);',sandbox);
 console.log('4) 连续上翻(第1页边界钳制): 页码 =',pg());
 // 数据变化(改当前页可见车辆价格) → 应重建。取全局最便宜的车(必在第1页)涨价，第一行会换成别的车
 vm.runInContext('const ci=DATA.reduce((m,d,i)=>(+d.price<+DATA[m].price?i:m),0);DATA[ci].price=+DATA[ci].price+50;render();',sandbox);
 console.log('5) 可见车辆价格变化: DOM变了 =',tb()!==snap1?'✅':'❌','| 行数 =',nRows(tb()));
 // 数据没变但翻页 → 重建当前页
 vm.runInContext('pgGo(2);',sandbox);
 console.log('6) 翻页重建: 页码 =',pg(),'| 行数 =',nRows(tb()));
 // 7) 用户在第3页时新命中出现 → 应自动跳回第1页 + 横幅 + 标题 + 命中车置顶
 vm.runInContext(`DATA=[{city:'广州市',store:'测试门店',title:"14'' BIKE 900 - 青玉色",quality:'S',price:499.9,retail:999.9,sku:'TESTHIT@S',image:''}].concat(DATA);render();`,sandbox);
 const hitFirst=tb().startsWith('<tr class="hit"');
 console.log('7) 第3页时新命中: 自动跳回第1页 =',pg().startsWith('第 1 ')?'✅':'❌(实际:'+pg()+')','| 横幅显示 =',sandbox.document.getElementById('hitbox').style.display==='block'?'✅':'❌','| 标题带警报 =',sandbox.document.title.includes('命中')?'✅':'❌','| 命中车置顶第一行 =',hitFirst?'✅':'❌');
 console.log('SIM DONE');
})().catch(e=>{console.error('SIM FAIL:',e.message);process.exit(1)});
