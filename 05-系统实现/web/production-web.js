/* production-web.js — 生产量级周计划：计划内场景组件（零依赖原生 JS）。
 * 不独立建单/不独立菜单。计划加载生产量级场景后，在 #/plan/{id} 由本组件渲染：
 * 计划依据(可折叠) / 生成调度方案 / 简洁状态(不含次数) /
 * 调度结果(4 方案业务名 + 完成率·整体按期交付率·已完成订单准时率·平均拖期·预计完成与未完成) /
 * 参考排程（单次模拟，按方案·产线·订单筛选分页）。
 * 预计指标为多情景评估的统计估计，非生产方案，不下发生产指令。 */
'use strict';
(() => {
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STRATS = ['EDD', 'COLOR_GROUP', 'LOAD_BALANCE', 'OPTIMIZED'];
const STRAT_LABEL = {EDD:'交期优先', COLOR_GROUP:'颜色集中', LOAD_BALANCE:'负荷均衡', OPTIMIZED:'综合优化'};
const STAGE_LABEL = {W:'焊装', P:'涂装', A:'总装'};
const NATURAL_DAY_SEC = 86400;
let userHeader = localStorage.getItem('aps.user') || 'planner-a';
let pollTimer = null, currentPlan = null, activeTaskId = null, lastResultId = null;
let orderState = { page:1, q:'' };
/* 轮询只读取任务状态快照（不计数、不回放事件）；离开计划/切身份即停。 */
let pollGen = 0;
let progState = null;
let pollLink = 'ok';
let pollErrN = 0;
const POLL_MS = 1000;
const POLL_ERR_MS = 1500;

function toast(){ return document.getElementById('toast'); }
function notify(msg, isErr=false){ const t=toast(); if(!t)return; t.textContent=msg; t.hidden=false; t.className='toast '+(isErr?'error':''); clearTimeout(notify._t); notify._t=setTimeout(()=>{ const tt=toast(); if(tt) tt.hidden=true; },4200); }
function el(tag, cls, text){ const n=document.createElement(tag); if(cls)n.className=cls; if(text!=null)n.textContent=text; return n; }
function card(title, body){ const s=el('section','card'); s.appendChild(el('h2',null,title)); s.appendChild(body); return s; }
function note(text, kind){ const d=el('div','prod-note '+(kind||'info')); d.textContent=text; return d; }
function btn(label, act, cls='', disabled=false, attrs){ const b=document.createElement('button'); b.type='button'; b.className='btn '+cls; b.textContent=label; b.setAttribute('data-pa',act); if(attrs) Object.keys(attrs).forEach(k=>b.setAttribute('data-'+k, attrs[k])); if(disabled)b.disabled=true; return b; }
function linkBtn(label, act, attrs){ return btn(label, act, '', false, attrs); }
function table(headers, rows){
  const wrap=el('div','table-scroll'); const t=el('table'); const thead=el('thead'); const hr=el('tr');
  headers.forEach(h=>hr.appendChild(el('th',null,h))); thead.appendChild(hr);
  const tb=el('tbody');
  if(!rows.length){ const tr=el('tr'); const td=el('td','empty'); td.colSpan=headers.length; td.textContent='暂无数据'; tr.appendChild(td); tb.appendChild(tr); }
  else rows.forEach(r=>{ const tr=el('tr'); (r||[]).forEach(c=>{ const td=el('td'); if(c==null){ td.textContent=''; } else if(typeof c==='string'){ td.innerHTML=c; } else if(typeof c==='object' && typeof c.appendChild==='function'){ td.appendChild(c); } else { td.textContent=String(c); } tr.appendChild(td); }); tb.appendChild(tr); });
  wrap.appendChild(thead); wrap.appendChild(tb); return wrap;
}
function details(title, body, open=false){ const d=el('details'); if(open)d.open=true; d.appendChild(el('summary',null,title)); d.appendChild(body); return d; }

/* ============ 业务口径的数值格式化（不暴露统计学术语） ============ */
function pct(x){ return (x==null||!isFinite(Number(x)))?'—':(Number(x)*100).toFixed(1)+'%'; }
function dayOf(sec){ return Math.floor(Math.max(0,Number(sec)||0)/NATURAL_DAY_SEC)+1; }
function hhmm(sec){ const s=Math.max(0,Math.round(Number(sec)||0)); const h=Math.floor((s%NATURAL_DAY_SEC)/3600), m=Math.floor((s%3600)/60); return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0'); }
function timeLabel(sec){ if(sec==null||!isFinite(Number(sec))) return '—'; return '第'+dayOf(sec)+'天 '+hhmm(sec); }
/* 平均拖期：按量级自动选分钟/小时，单位写清 */
function tardyLabel(secPerOrder){
  if(secPerOrder==null||!isFinite(Number(secPerOrder))) return '—';
  const min=Number(secPerOrder)/60;
  if(min<1) return '<1 分钟';
  return min<120 ? (min.toFixed(1)+' 分钟') : ((min/60).toFixed(1)+' 小时');
}
function qtyLbl(x, unit){ return (x==null||!isFinite(Number(x)))?'—':(Math.round(Number(x))+' '+unit); }

async function api(path, method='GET', body){
  const r=await fetch('/api'+path,{method, headers:{'Content-Type':'application/json','X-Demo-User':userHeader}, ...(body?{body:JSON.stringify(body)}:{})});
  const data=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.error||('请求失败 '+r.status));
  return data;
}

/* ============ 任务的本地视图状态 ============ */
let _curTask=null;
function currentTask(){ return _curTask; }
function scFromPlan(){ return (currentPlan&&currentPlan.input&&currentPlan.input.scenario)||{}; }

/* ============ 生成调度方案：状态 + 操作（不含次数/失败数/种子/事件） ============ */
function renderTaskCard(){
  let host=document.querySelector('[data-task-root]');
  if(!host){ host=el('div',''); host.setAttribute('data-task-root',''); const v=document.getElementById('view'); if(v) v.appendChild(host); }
  host.textContent='';
  const t=currentTask();
  if(!t){
    host.appendChild(note('尚未生成调度方案。点击下方开始计算。','info'));
    const row=el('div','prod-actions'); row.appendChild(btn('开始计算','run','primary')); host.appendChild(row);
    return;
  }
  host.appendChild(buildLivePanel());
  const tlRow=el('div','prod-actions');
  tlRow.appendChild(linkBtn('查看参考排程（单次模拟）','timeline-open',{strategy:'OPTIMIZED'}));
  host.appendChild(tlRow);
  const req = !!(t.progress && t.progress.cancel_requested);
  const live = ['queued','running','cancelling'].includes(t.status) || (t.status==='running' && req);
  if(t.status==='cancelling' || (t.status==='running' && req)){
    host.appendChild(note('取消中：正在停止计算，完成后状态将如实显示。','warn'));
  } else if(t.status==='queued' || t.status==='running'){
    const row=el('div','prod-actions'); row.appendChild(btn('取消','cancel')); host.appendChild(row);
  }
  else if(t.status==='completed'){ openResult(t.id, currentPlan); }
  else if(t.status==='cancelled'){ host.appendChild(note('计算已取消，未生成调度方案。可重新开始计算。','warn')); }
  else if(t.status==='failed'){ host.appendChild(note('计算未完成：'+(t.error||'服务中断或引擎异常')+'。可重新开始计算。','bad')); }
  patchLive();
  if(live) startPolling(t.id, currentPlan && currentPlan.id);
}

/* ============ 简洁状态面板（业务语言；无计数、无失败数、无种子、无事件序号、无用时） ============ */
function buildLivePanel(){
  const p=el('div','prod-status'); p.setAttribute('data-live-panel','');
  const line=el('div','prod-status-line');
  const ind=el('span','prod-run-dot'); ind.setAttribute('data-p','runind'); ind.setAttribute('role','img'); line.appendChild(ind);
  const txt=el('strong','prod-status-text','—'); txt.setAttribute('data-p','status');
  txt.setAttribute('role','status'); txt.setAttribute('aria-live','polite'); line.appendChild(txt);
  p.appendChild(line);
  const bar=document.createElement('progress');
  bar.className='prod-native-progress'; bar.setAttribute('data-p','bar'); bar.max=1; bar.value=0;
  bar.setAttribute('aria-label','调度方案计算进度');
  p.appendChild(bar);
  const pn=el('div','prod-phase-note'); pn.setAttribute('data-p','note'); p.appendChild(pn);
  return p;
}
const PHASE_TEXT = {nominal:'准备基准排程', sampling:'多情景评估中', completed:'计算完成', cancelled:'已取消', failed:'计算失败'};
/* 只更新字段、不重建 DOM；面板不存在（已离开计划页面）返回 false */
function patchLive(){
  const panel=document.querySelector('[data-live-panel]'); if(!panel) return false;
  const live=progState||{};
  const status=live.status||((currentTask()||{}).status)||'';
  const phase=live.phase||(currentTask()&&currentTask().progress&&currentTask().progress.phase)||'';
  const running=['queued','running','cancelling'].includes(status);
  let statusText;
  if(pollLink==='waiting') statusText='状态更新中断，等待恢复';
  else if(!status) statusText='—';
  else if(status==='queued') statusText='排队中，等待开始';
  else if(status==='cancelling') statusText='取消中，正在停止计算';
  else if(status==='running') statusText=(phase==='nominal'?'准备基准排程':'多情景评估中');
  else if(status==='completed') statusText='计算完成';
  else if(status==='cancelled') statusText='已取消';
  else if(status==='failed') statusText='计算失败';
  else statusText='—';
  setP(panel,'status',statusText);
  const bar=panel.querySelector('[data-p="bar"]');
  if(bar){
    if(status==='completed'){ bar.max=1; bar.value=1; }
    else if(running || status==='cancelling'){ bar.removeAttribute('value'); } // 不确定进度（不显示次数）
    else { bar.max=1; bar.value=0; }
  }
  const pn=panel.querySelector('[data-p="note"]');
  if(pn){
    let txt='';
    if(pollLink==='waiting') txt='与计算服务的连接中断，等待恢复；恢复后自动继续显示状态。';
    else if(status==='queued') txt='已排入计算队列，稍后开始。';
    else if(status==='cancelling') txt='已下发取消，正在停止计算。';
    else if(status==='running' && phase==='nominal') txt='正在准备基准排程，随后进行多情景评估。';
    else if(status==='running') txt='正在按多种情景评估 4 个调度方案，完成后自动展示调度结果。';
    else if(status==='completed') txt='调度结果已生成。预计指标为多情景评估的统计估计（预测/仿真估计）。';
    else if(status==='cancelled') txt='计算已取消，未生成调度结果。';
    else if(status==='failed') txt='计算未完成，可重新开始计算。';
    pn.className='prod-phase-note'+(pollLink==='waiting'?' wait':'');
    pn.textContent=txt;
  }
  const ind=panel.querySelector('[data-p="runind"]');
  if(ind){
    const active=(status==='running'||status==='queued') && pollLink==='ok';
    ind.className='prod-run-dot'+(active?' on':(pollLink==='waiting'?' wait':''));
    ind.setAttribute('aria-label',active?'正在计算':(pollLink==='waiting'?'状态更新中断':'已停止'));
  }
  return true;
}
function setP(root,key,text){ const n=root.querySelector('[data-p="'+key+'"]'); if(n) n.textContent=text; }

/* ============ 生产量级周计划页面 ============ */
async function renderProduction(plan){
  stopPolling(); currentPlan=plan; orderState={page:1,q:''}; _curTask=null; activeTaskId=null;
  progState=null; pollLink='ok'; pollErrN=0;
  const v=document.getElementById('view');
  try{
    await api('/me').catch(()=>({role:'planner'}));
    const prods = plan.production_tasks||plan.tasks||[];
    const running = prods.find(t=>['queued','running','cancelling'].includes(t.status));
    const latestDone = [...prods].reverse().find(t=>['completed','failed','cancelled'].includes(t.status));
    _curTask = running||latestDone||null;
    if(_curTask){ activeTaskId=_curTask.id; if(_curTask.status==='completed') lastResultId=_curTask.id; }

    v.innerHTML='';
    const head=el('div','page-head');
    const back=el('a','btn'); back.href='#/plans'; back.textContent='返回计划管理'; head.appendChild(back);
    head.appendChild(el('h1',null,esc(plan.name)+' · 生产量级周计划'));
    v.appendChild(head);
    v.appendChild(note('演示环境：结果为仿真预测，未接入生产系统，未下发生产指令。','warn'));

    v.appendChild(card('计划依据', details('查看计划规模、产能节拍与订单明细', inputSummary(), false)));
    const taskBody=el('div',''); taskBody.setAttribute('data-task-root','');
    v.appendChild(card('生成调度方案', taskBody));
    const tl=el('div',''); tl.id='tl-panel'; v.appendChild(tl);
    renderTaskCard();
  }catch(e){
    v.appendChild(note('生产量级周计划渲染失败：'+((e&&e.message)||String(e)),'bad'));
  }
}

/* ============ 计划依据（业务明细，可折叠；不含种子/扰动/原始数据/技术日志） ============ */
function inputSummary(){
  const sc=scFromPlan(); const plan=currentPlan||{};
  const w=el('div');
  const cal=sc.calendar||{};
  const nVehicles = sc.n_vehicles!=null?sc.n_vehicles:4200;
  w.appendChild(note('生产量级周计划：'+nVehicles+' 辆 · 工作日 '+(cal.workdays||5)+' 天 · 每日 '+(cal.shifts_per_day||2)+' 班 · 每班 '+(cal.shift_min||460)+' 分钟 · 焊装 → 涂装 → 总装串行，有限缓冲。','info'));
  const grid=el('div','prod-metric-grid');
  const stageName={W:'焊装',P:'涂装',A:'总装'};
  const buf = sc.buffer_cap ? Object.entries(sc.buffer_cap).map(([k,v])=>(stageName[k[0]]||k[0])+'→'+(stageName[k[2]]||k[2])+'='+v+' 辆').join('，') : '—';
  [['计划车辆',nVehicles+' 辆'],['工厂',plan.factory||'—'],['工作天数',(cal.workdays||5)+' 天'],['每日班次',(cal.shifts_per_day||2)+' 班'],['班次时长',(cal.shift_min||460)+' 分钟'],['工序间缓冲',buf]].forEach(([k,v])=>{
    const m=el('div','prod-metric'); m.appendChild(el('span',null,k)); m.appendChild(el('strong',null,esc(v==null?'—':v))); grid.appendChild(m); });
  w.appendChild(grid);
  // 产能来源：各配置各工序等效节拍（秒）
  const cap=(sc.capacity_sources||[]).map(c=>{const t=c.takt_sec||{};return [esc(c.config),esc(c.model||'—'),esc(c.color||'—'),t.W!=null?Math.round(t.W):'—',t.P!=null?Math.round(t.P):'—',t.A!=null?Math.round(t.A):'—'];});
  w.appendChild(details('产能节拍（各配置各工序等效节拍，秒）', table(['配置','车型','颜色','焊装','涂装','总装'],cap), false));
  // 订单（业务明细，分页到末页）
  const od=el('div');
  const search=el('input'); search.type='search'; search.placeholder='按车号/配置/车型搜索'; search.id='prod-order-q';
  const frow=el('div','filters'); const fl=el('label'); fl.appendChild(el('span',null,'订单搜索')); fl.appendChild(search); frow.appendChild(fl); frow.appendChild(btn('搜索','orders-search'));
  const oh=el('div'); oh.id='orders-box';
  od.appendChild(frow); od.appendChild(oh);
  w.appendChild(details('订单明细（分页查看，可到末页）', od, false));
  setTimeout(()=>loadOrders(1),0);
  return w;
}
async function loadOrders(page){
  const host=document.getElementById('orders-box'); if(!host || !currentPlan) return;
  host.textContent='加载中…';
  try{
    const searchInput=document.getElementById('prod-order-q');
    const q=searchInput ? searchInput.value : (orderState.q||'');
    orderState.q=q;
    const d=await api('/plans/'+currentPlan.id+'/production-orders?page='+page+'&per_page=100&q='+encodeURIComponent(q));
    orderState.page=d.page;
    const head=el('div','filters');
    head.appendChild(el('span',null,'共 '+d.total+' 条 · 页 '+d.page+'/'+d.n_pages+(q?(' · 搜索"'+esc(q)+'"'):'')));
    if(d.page>1) head.appendChild(btn('上一页','orders-prev'));
    if(d.page<d.n_pages) head.appendChild(btn('下一页','orders-next'));
    head.appendChild(btn('末页','orders-last'));
    const rows=(d.orders||[]).map(o=>[esc(o.vin),esc(o.model),esc(o.color),esc(o.config),esc(o.priority==null?'—':o.priority),timeLabel(o.release_at_sec),timeLabel(o.due_at_sec)]);
    host.textContent=''; host.appendChild(head);
    host.appendChild(table(['车号','车型','颜色','配置','优先级','释放时间','交付交期'],rows));
  }catch(e){ host.textContent=''; host.appendChild(note('订单加载失败：'+e.message,'bad')); }
}

/* ============ 启动 / 取消 / 状态轮询 ============ */
async function startRun(){
  if(!currentPlan) return notify('无计划','info');
  try{ const r=await api('/plans/'+currentPlan.id+'/run','POST',{}); notify('已开始计算调度方案'); activeTaskId=r.task_id; refreshPlan(); }catch(e){ notify(e.message,true); }
}
async function cancelRun(){
  if(!activeTaskId || !currentPlan) return notify('暂无进行中的计算','info');
  if(_curTask){ _curTask.status='cancelling'; _curTask.progress=Object.assign({}, _curTask.progress||{}, {cancel_requested:true}); renderTaskCard(); }
  const tid=activeTaskId, pid=currentPlan.id;
  try{ await api('/plans/'+pid+'/production-cancel','POST',{task:tid}); notify('已下发取消，正在停止计算。'); }
  catch(e){ notify(e.message,true); }
  if(currentPlan && currentPlan.id===pid) startPolling(tid, pid);
}
async function refreshPlan(){
  try{ const d=await api('/plans/'+currentPlan.id); renderProduction(d.plan||currentPlan); }catch(e){ notify(e.message,true); }
}

/* ============ 状态轮询：只取任务状态快照（不计数、不回放事件） ============ */
function stopPolling(){ pollGen++; if(pollTimer){ clearTimeout(pollTimer); pollTimer=null; } }
function onPlanView(planId){
  if(!currentPlan || currentPlan.id!==planId) return false;
  if((localStorage.getItem('aps.user')||'planner-a')!==userHeader) return false;
  return String(location.hash||'').indexOf(planId)>=0;
}
function startPolling(tid, planId){
  stopPolling();
  const gen=pollGen;
  activeTaskId=tid;
  pollTick(gen, tid, planId);
}
async function pollTick(gen, tid, planId){
  if(gen!==pollGen) return;
  if(!onPlanView(planId)){ stopPolling(); return; }
  let d=null;
  try{
    d=await api('/plans/'+encodeURIComponent(planId)+'/production-progress?task='+encodeURIComponent(tid));
  }catch(e){
    if(gen!==pollGen) return;
    if(!onPlanView(planId)){ stopPolling(); return; }
    pollLink='waiting'; pollErrN+=1;
    patchLive();
    pollTimer=setTimeout(()=>pollTick(gen,tid,planId), Math.min(POLL_ERR_MS*pollErrN, 4000));
    return;
  }
  if(gen!==pollGen) return;
  if(!onPlanView(planId)){ stopPolling(); return; }
  pollLink='ok'; pollErrN=0;
  progState=Object.assign({}, progState||{}, {
    status:d.status, phase:d.phase, error:d.error,
  });
  if(_curTask){
    _curTask=Object.assign({}, _curTask, {status:d.status, error:d.error||_curTask.error,
      progress:Object.assign({}, _curTask.progress||{}, {
        phase:progState.phase, cancel_requested:(d.status==='cancelling')||!!((_curTask.progress||{}).cancel_requested),
      })});
  }
  patchLive();
  if(d.status==='completed'){ stopPolling(); openResult(tid, currentPlan); return; }
  if(d.status==='failed'||d.status==='cancelled'){ stopPolling(); refreshPlan(); return; }
  pollTimer=setTimeout(()=>pollTick(gen,tid,planId), POLL_MS);
}

/* ============ 参考排程（单次模拟；按方案·产线·订单查看） ============ */
let tlState={ strategy:'OPTIMIZED', stage:'', q:'', page:1 };
async function openTimeline(init){
  if(!currentPlan) return;
  tlState=Object.assign({}, tlState, init||{});
  const tid=activeTaskId||lastResultId;
  if(!tid) return notify('暂无计算结果','info');
  let host=document.getElementById('tl-panel');
  if(!host){ host=el('div',''); host.id='tl-panel'; document.getElementById('view').appendChild(host); }
  host.textContent='';
  const c=el('section','card'); c.appendChild(el('h2',null,'参考排程（单次模拟）'));
  c.appendChild(note('单次模拟得到的排程参考，不是平均排程，也不是生产指令；预计指标来自多情景评估的统计估计。','info'));
  const f=el('div','filters');
  const stLabel=el('label'); stLabel.appendChild(el('span',null,'方案')); const stSel=el('select'); stSel.id='tl-strategy'; STRATS.forEach(s=>{const o=el('option'); o.value=s; o.textContent=STRAT_LABEL[s]; if(s===tlState.strategy)o.selected=true; stSel.appendChild(o);}); stLabel.appendChild(stSel); f.appendChild(stLabel);
  const gLabel=el('label'); gLabel.appendChild(el('span',null,'产线')); const gSel=el('select'); gSel.id='tl-stage'; [['','全部'],['W','焊装'],['P','涂装'],['A','总装']].forEach(([v,t])=>{const o=el('option'); o.value=v; o.textContent=t; if(v===tlState.stage)o.selected=true; gSel.appendChild(o);}); gLabel.appendChild(gSel); f.appendChild(gLabel);
  const qLabel=el('label'); qLabel.appendChild(el('span',null,'订单搜索')); const qIn=el('input'); qIn.type='search'; qIn.value=tlState.q; qIn.id='tl-q'; qLabel.appendChild(qIn); f.appendChild(qLabel);
  f.appendChild(btn('查询','timeline-query'));
  c.appendChild(f);
  const body=el('div'); body.id='tl-body'; c.appendChild(body);
  host.appendChild(c);
  loadTimeline();
}
async function loadTimeline(){
  const tid=activeTaskId||lastResultId; const body=document.getElementById('tl-body'); if(!body||!currentPlan) return;
  body.textContent='加载中…';
  try{
    const qs='task='+encodeURIComponent(tid)+'&strategy='+tlState.strategy+
             '&stage='+encodeURIComponent(tlState.stage)+'&q='+encodeURIComponent(tlState.q)+'&page='+tlState.page+'&per_page=100';
    const d=await api('/plans/'+currentPlan.id+'/production-timeline?'+qs);
    body.textContent='';
    const head=el('div','filters');
    head.appendChild(el('span',null,'方案 '+STRAT_LABEL[d.strategy]+(d.stage_filter?(' · 产线 '+STAGE_LABEL[d.stage_filter]):'')+' · 共 '+d.total+' 行 · 页 '+d.page+'/'+d.n_pages));
    if(d.page>1) head.appendChild(btn('上一页','tl-prev'));
    if(d.page<d.n_pages) head.appendChild(btn('下一页','tl-next'));
    body.appendChild(head);
    body.appendChild(table(['车辆','产线','开始时间','结束时间','时长（分钟）'], (d.rows||[]).map(e=>{
      const dur=(e.finish!=null && e.start!=null && isFinite(Number(e.finish)))?((Number(e.finish)-Number(e.start))/60).toFixed(1):'—';
      return [esc(e.vin),esc(STAGE_LABEL[e.stage]||e.stage),timeLabel(e.start),timeLabel(e.finish),dur];
    })));
  }catch(e){ body.textContent=''; body.appendChild(note('参考排程加载失败：'+e.message,'bad')); }
}

/* ============ 调度结果（4 方案业务口径对比；预计指标为统计估计） ============ */
async function openResult(tid, plan){
  activeTaskId=tid; lastResultId=tid;
  try{
    const data=await api('/plans/'+plan.id+'/production-result?task='+tid);
    const v=document.getElementById('view'); let box=document.getElementById('stats-box');
    if(!box){ const c=document.createElement('section'); c.className='card'; c.appendChild(el('h2',null,'调度结果')); box=el('div',''); box.id='stats-box'; c.appendChild(box); v.appendChild(c); }
    renderStats(data.result||data, box, plan);
  }catch(e){ const b=document.querySelector('#stats-box'); if(b){ b.innerHTML=''; b.appendChild(note('调度结果读取失败：'+e.message,'bad')); } }
}
function stratMetrics(p, nVehicles){
  const stat=(p&&p.stat)||{};
  const meanOf=k=>{ const s=stat[k]; return (s&&s.mean!=null)?Number(s.mean):null; };
  const ok=p&&p.denominator!=null?p.denominator:(p&&p.ok)||0;
  const completed = ok ? (Number(p.completed_total||0)/ok) : null;
  return {
    completionRate: meanOf('completion_rate'),
    overallOnTime: meanOf('overall_on_time_rate'),
    conditionalOnTime: meanOf('conditional_on_time_rate'),
    avgTardySecPerOrder: meanOf('mean_tardy_over_completed_sec'),
    estCompleted: completed,
    estUnmet: (completed!=null && nVehicles!=null) ? Math.max(0, nVehicles-completed) : null,
  };
}
function renderStats(result, box, plan){
  box.textContent='';
  const r=result&&result.result?result.result:result;
  const cfg=r.config||{};
  const nVehicles = cfg.n_vehicles!=null?cfg.n_vehicles:(scFromPlan().n_vehicles!=null?scFromPlan().n_vehicles:4200);
  box.appendChild(note('预计为多情景评估的统计估计（预测/仿真估计），非实际生产结果，也未下发生产指令。','warn'));
  const grid=el('div','prod-metric-grid');
  [['计划车辆',nVehicles+' 辆'],['评估方案','4 个']].forEach(([k,v])=>{
    const m=el('div','prod-metric'); m.appendChild(el('span',null,k)); m.appendChild(el('strong',null,esc(v))); grid.appendChild(m); });
  box.appendChild(grid);

  const head=['方案','预计完成率','整体按期交付率','已完成订单准时率','平均拖期（每完成订单）','预计完成','预计未完成'];
  const rows=STRATS.map(st=>{
    const p=(r.per_strategy||{})[st]; if(!p) return null;
    const m=stratMetrics(p, nVehicles);
    return [esc(STRAT_LABEL[st]), pct(m.completionRate), pct(m.overallOnTime), pct(m.conditionalOnTime),
            tardyLabel(m.avgTardySecPerOrder), qtyLbl(m.estCompleted,'辆'), qtyLbl(m.estUnmet,'辆')];
  }).filter(Boolean);
  box.appendChild(table(head, rows));
  box.appendChild(note('口径：预计完成率 = 预计完成车辆 / 计划车辆；整体按期交付率 = 按期完成车辆 / 计划车辆；已完成订单准时率 = 按期完成车辆 / 已完成车辆（仅统计已完成订单，未完成不计入分母）；平均拖期为全部已完成订单的平均拖期时长（含按期订单的 0 拖期）。','info'));
  box.appendChild(note('说明：当前结果用于方案对比评估，未生成本次的生产指令。','info'));
}

/* ==================== public API / 事件委托 ==================== */
function route(){ /* 并入 #/plan/{id} 渲染，独立页面已移除 */ }
window.APSProduction = {
  route, renderProduction,
  handleAction(act, e2){
    const ds=k => (e2 && e2.getAttribute ? e2.getAttribute(k) : null);
    switch(act){
      case 'run': startRun(); break;
      case 'cancel': cancelRun(); break;
      case 'orders-search': loadOrders(1); break;
      case 'orders-prev': loadOrders(Math.max(1,(orderState.page||1)-1)); break;
      case 'orders-next': loadOrders((orderState.page||1)+1); break;
      case 'orders-last': loadOrdersLast(); break;
      case 'timeline-open': openTimeline({ strategy:ds('data-strategy')||tlState.strategy }); break;
      case 'timeline-query': { const st=document.getElementById('tl-strategy'), g=document.getElementById('tl-stage'), q=document.getElementById('tl-q');
                               openTimeline({ strategy:st?st.value:'OPTIMIZED', stage:g?g.value:'', q:q?q.value:'', page:1 }); break; }
      case 'tl-prev': tlState.page=Math.max(1,tlState.page-1); loadTimeline(); break;
      case 'tl-next': tlState.page=tlState.page+1; loadTimeline(); break;
    }
  }
};
async function loadOrdersLast(){
  const host=document.getElementById('orders-box'); if(!host || !currentPlan) return;
  try{ const q=orderState.q||''; const d=await api('/plans/'+currentPlan.id+'/production-orders?page=1&per_page=1&q='+encodeURIComponent(q)); await loadOrders(Math.max(1,Math.ceil((d.total||0)/100))); }catch(e){ }
}
document.addEventListener('click', e=>{
  const t=e.target.closest('[data-pa]');
  if(t){ e.preventDefault(); const a=t.getAttribute('data-pa'); if(window.APSProduction&&window.APSProduction.handleAction) window.APSProduction.handleAction(a,t); }
});
})();