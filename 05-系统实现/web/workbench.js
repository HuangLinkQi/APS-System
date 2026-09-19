'use strict';
(() => {
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stage = {W:'焊装',P:'涂装',A:'总装'};
const statuses = {draft:'草稿',running:'计算中',ready:'计算完成',failed:'计算失败',pending:'待审批',approved:'已批准'};
const strategies = {Strategy_A_EDD:'交期优先',Strategy_B_ColorAware:'颜色集中',Strategy_D_Smoothing:'负荷均衡',Strategy_E_ALNS:'综合优化'};
const users = {'planner-a':'计划员 A · F1','planner-b':'计划员 B · F2',engineer:'工艺负责人 · F1',manager:'生管主管',admin:'系统管理员'};
let me, plans=[], catalogs=[], scenarios=[], plan=null, selected='', task=null, timer=null, epoch=0, busy=false, returnPlan='', catalogId='', modalFocus=null, toastTimer=null;
let user=localStorage.getItem('aps.user') || 'planner-a';
if(!users[user]) user='planner-a';
const canEdit = () => me?.role==='planner' && plan && ['draft','ready','failed'].includes(plan.status);
const btn = (label, act, data='', cls='', disabled=false) => `<button type="button" class="btn ${cls}" data-act="${act}" ${data} ${disabled?'disabled':''}>${esc(label)}</button>`;
const link = (label,url) => `<a class="btn" href="${esc(url)}">${esc(label)}</a>`;
const note = (text,kind='info') => `<div class="note ${kind}">${esc(text)}</div>`;
const card = (title,body) => `<section class="card"><h2>${esc(title)}</h2>${body}</section>`;
const table = (headers,rows) => `<div class="table-scroll"><table><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(r=>`<tr>${r.map(x=>`<td>${x}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${headers.length}" class="empty">暂无记录</td></tr>`}</tbody></table></div>`;
const objectTable = rows => rows.length ? table([...new Set(rows.flatMap(Object.keys))],rows.map(r=>[...new Set(rows.flatMap(Object.keys))].map(k=>esc(r[k] ?? '—')))) : '<p class="muted">无记录</p>';
const details = (title,body,open=false) => `<details ${open?'open':''}><summary>${esc(title)}</summary>${body}</details>`;
function notify(text,error=false){ clearTimeout(toastTimer); $('#toast').textContent=text; $('#toast').hidden=false; $('#toast').className='toast '+(error?'error':''); toastTimer=setTimeout(()=>{$('#toast').hidden=true;},4000); }
async function api(path,method='GET',body){
 const response=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json','X-Demo-User':user},...(body?{body:JSON.stringify(body)}:{})});
 const data=await response.json(); if(!response.ok)throw new Error(data.error||'操作失败');return data;
}
function modal(title,body){ modalFocus=document.activeElement; $('#modal').hidden=false; $('#modal').innerHTML=`<div class="backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><h2 id="dialog-title">${esc(title)}</h2><div id="dialog-error" role="alert"></div>${body}</section></div>`; $('#modal').querySelector('input,button,select')?.focus(); }
function closeModal(){ $('#modal').hidden=true;$('#modal').innerHTML='';modalFocus?.focus(); }
function field(label,name,value='',type='text',extra=''){return `<label class="field">${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;}
function shell(){
 const nav=me.role==='admin'?[['system','系统说明']]:[['plans','计划管理'],['rules','基础数据'],...(me.role==='manager'?[['inbox','待审批']]:[])];
 $('#nav').innerHTML=nav.map(([id,label])=>`<a href="#/${id}" class="${location.hash.includes('/'+id)?'active':''}">${label}</a>`).join('');
 $('#scopeInfo').textContent=`${users[user]} · ${me.factories.join(' / ')||'无业务权限'}`;
 $('#envText').textContent='演示服务已连接';$('#envDot').className='dot ok';
 $('#userSelect').innerHTML=Object.entries(users).map(([id,label])=>`<option value="${id}" ${id===user?'selected':''}>${label}</option>`).join('');
}
async function refresh(){
 const token=++epoch;clearTimeout(timer);clearTimeout(toastTimer);task=null;$('#toast').hidden=true;
 $('#view').innerHTML=card('加载中','<p class="muted">正在读取数据…</p>');
 try{
  me=await api('/me'); if(token!==epoch)return;
  shell();
  const parts=location.hash.replace(/^#\//,'').split('/'); const view=parts[0]||'plans';
  $('#crumb').textContent=({plans:'计划管理',plan:'排程工作台',prod:'生产量级仿真',rules:'基础数据',inbox:'待审批',system:'系统说明'})[view]||'计划管理';
  if(view==='system'){if(me.role!=='admin')throw new Error('当前身份无系统管理权限');renderSystem();return;}
  if(me.role==='admin')throw new Error('管理员无业务数据权限，请进入系统说明');
  if(view==='plan'){
   const data=await api('/plans/'+encodeURIComponent(parts[1]));if(token!==epoch)return;plan=data.plan;selected=plan.selected_candidate||plan.result?.selected_strategy||'';
   if(parts[2]==='input'){const data=await api('/plans/'+plan.id+'/business-input');if(token!==epoch)return;renderInput(data);}
   else {renderPlan();if(plan.status==='running'&&plan.tasks?.length)poll(plan.tasks[0].id,token);}
  }else if(view==='rules'){
   const data=await api('/catalogs');if(token!==epoch)return;catalogs=data.catalogs;if(parts[1])catalogId=decodeURIComponent(parts[1]);renderMasters();
  }else{
   if(view==='inbox'&&me.role!=='manager')throw new Error('仅主管可查看待审批');
   const data=await api('/plans');if(token!==epoch)return;plans=data.plans;renderList(view==='inbox');
  }
 }catch(e){if(token!==epoch)return;$('#view').innerHTML=card('无法打开',note(e.message,'bad')+link('返回可用入口',me?.role==='admin'?'#/system':'#/plans'));}
}
function renderList(inbox=false){
 $('#view').innerHTML=`<div class="page-head"><div><h1>${inbox?'待审批':'计划管理'}</h1><p>从业务数据生成排程方案。无预置计划，无预置计算结果。</p></div>${!inbox&&me.role==='planner'?btn('新建排程','new','','primary'):''}</div>`+
 `<div class="filters"><label>搜索计划<input id="search" type="search" placeholder="输入名称或编号"></label><label>状态<select id="status-filter"><option value="">全部状态</option>${Object.entries(statuses).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label>${btn('刷新列表','refresh')}</div><div id="plan-list"></div>`;
 const draw=()=>{const q=$('#search').value.toLowerCase(), status=$('#status-filter').value;const rows=plans.filter(p=>(!inbox||p.status==='pending')&&(!status||p.status===status)&&(!q||(p.name+' '+p.id).toLowerCase().includes(q)));
 $('#plan-list').innerHTML=card(`计划列表 · ${rows.length} 条`,table(['计划名称','工厂','状态','数据来源','历史计算','操作'],rows.map(p=>[esc(p.name),esc(p.factory),`<span class="status ${p.status}">${statuses[p.status]||esc(p.status)}</span>`,esc(p.input?.source_name || (p.production||p.type==='PRODUCTION_WEEKLY'?'生产量级周计划':(p.input?'历史版本输入':'尚未选择'))),esc((p.tasks||[]).length),link('打开',`#/plan/${p.id}`)]))+(!rows.length?note('暂无计划。通过"新建排程"开始；系统不会自动生成演示计划。'):''));};
 $('#search').addEventListener('input',draw);$('#status-filter').addEventListener('change',draw);draw();
}
function renderPlan(){
 if(plan.production||plan.type==='PRODUCTION_WEEKLY'){ $('#crumb').textContent='生产量级周计划'; $('#crumbSub').textContent=plan.name||''; $('#scopeInfo').textContent=users[user]+' · '+me.factories.join(' / ')||' '; if(window.APSProduction&&window.APSProduction.renderProduction){window.APSProduction.renderProduction(plan);return;} }
 const inp=plan.input;const checks=inp?.checks||[];const editable=canEdit();const candidates=plan.result?.candidates||{};
 if(!candidates[selected])selected=Object.keys(candidates)[0]||'';
 $('#view').innerHTML=`<div class="page-head"><div><a href="#/plans">返回计划管理</a><h1>${esc(plan.name)}</h1><p>${esc(plan.factory)} · W+3演示排程 · <span class="status ${plan.status}">${statuses[plan.status]||esc(plan.status)}</span></p></div>${btn('刷新计划','refresh')}</div>`+
 note('研究演示环境：未接入ERP / MES。提交和批准仅记录本机演示状态，不下发生产指令。','warn')+
 card('1 · 准备数据',inp?`<div class="ready-summary"><b>${esc(inp.source_name||'历史输入')}</b><span>基础资料版本 ${esc(inp.master_revision||'历史版本')} · 输入于 ${esc(inp.loaded_at||'—')}</span></div>`+
 (checks.length?note('数据待完善：'+checks.join('；'),'bad'):note('本次输入已准备完成，无需重复填写产能、节拍或工艺规则。','ok'))+
 `<div class="actions">${link('查看本次输入',`#/plan/${plan.id}/input`)}${btn('前往基础数据','to-master')}${editable?btn('引用最新基础数据','refresh-master')+btn('更换演示场景','choose'):''}</div>`:
 note('尚未选择演示场景。选择后将一次准备订单、供应、产线、节拍和日历。')+(editable?btn('下一步：准备数据','choose','','primary'):''))+
 card('2 · 生成方案',`<p>系统比较交期、颜色集中、负荷均衡和综合优化四种候选，不要求输入算法参数。</p>`+
 (editable?btn('生成方案','run','','primary',!inp||checks.length>0):'')+
 `<div id="progress" role="status">${plan.status==='running'?note('正在计算，请等待真实计算结果。'):plan.status==='failed'?note(plan.tasks?.[0]?.error||'上次计算失败，请检查输入后重试。','bad'):''}</div>`+
 (!editable?'<p class="muted">当前角色或计划状态不允许修改输入和重新计算。</p>':''))+
 (plan.result?(['pending','approved'].includes(plan.status)
  ? card('3 · 查看与复核方案',note('方案已提交，显示页面锁定在选定候选，仅供核对，不可更换或重算。','warn')+resultBody(plan.result.candidates[selected]||{}))
  : card('3 · 查看并选择方案',`<div class="candidate-tabs">${Object.keys(candidates).map(k=>btn(strategies[k]||k,'candidate',`data-key="${esc(k)}"`,k===selected?'primary':'')).join('')}</div><div id="candidate"></div>`))
 : card('3 · 方案结果','<div class="empty">尚无计算结果。不会展示预填的调度顺序或指标。</div>'))+
 card('4 · 提交与审批',plan.status==='pending'?note('已提交，等待主管审批。选定方案：'+(strategies[plan.selected_candidate]||plan.selected_candidate),'ok'):
 plan.status==='approved'?note(`已批准 · ${users[plan.approved_by]||plan.approved_by} · ${plan.approved_at}。仅演示记录，不是生产指令。`,'ok'):
 me.role==='planner'&&plan.result?`<p>选择可提交候选后交给主管审批。未完成车辆或校验不通过将阻断提交。</p><div id="submit-action"></div>`:'<p class="muted">计算并选择方案后可提交审批。</p>')+
 (me.role==='manager'&&plan.status==='pending'?btn('批准该方案','approve','','primary'):'')+
 card('计算历史',table(['任务编号','状态','开始时间','操作'],(plan.tasks||[]).map(t=>[esc(t.id),esc(t.status==='completed'?'已完成':t.status==='failed'?'失败':t.status),esc(t.created_at),btn('查看历史计算','history',`data-id="${esc(t.id)}"`)])));
 if(plan.result&&!['pending','approved'].includes(plan.status))renderCandidate();
}
function metrics(c){const m=c.metrics||{};const unfc=c.unfinished_count ?? (c.unscheduled||[]).length;return `<div class="metric-grid">${[['未完成订单',unfc,'台（未排'+((c.unfinished_details||[]).filter(x=>x.kind==='未排入').length)+'+已排未完成'+((c.unfinished_details||[]).filter(x=>x.kind!=='未排入').length)+')'],['完成准时率',m.on_time_rate==null?'—':(Number(m.on_time_rate)*100).toFixed(1),'%'],['总拖期',m.true_weighted_tardiness_min??'—','分钟（加权）'],['换色次数',m.color_switches??'—','次'],['完工跨度',m.makespan_min??'—','分钟']].map(([n,v,u])=>`<div><span>${n}</span><strong>${esc(v)}</strong><small>${u}</small></div>`).join('')}</div>`;}
function resultBody(c){
 const seq=c.schedule?.sequence||[];
 const nodes=new Map();for(const e of c.events||[]){const key=e.car_id+'|'+e.stage; if(!nodes.has(key))nodes.set(key,{vin:e.car_id,stage:e.stage,resource:e.resource_id});nodes.get(key)[e.node]=e.timestamp_min;}
 const unmeta=(c.unfinished_details||(c.unscheduled||[]).map(v=>({vin:typeof v==='string'?v:JSON.stringify(v),kind:'未排入'})));
 return metrics(c)+note(c.validation?.valid?'当前模型独立校验通过；不代表完整工厂工艺均已覆盖。':'独立校验未通过',''+(c.validation?.valid?'ok':'bad'))+
 (c.submit_blockers?.length?note(c.submit_blockers.join('；'),'warn'):'')+
 details('焊装投入顺序 · '+seq.length+' 辆',table(['顺序','车辆','订单','车型','颜色','优先级','交期（第几天）'],seq.map(x=>[x.position,x.car_id,x.order_id,x.model,x.color,x.priority,(x.due_day??0)+1].map(esc))),true)+
 details('各工序时间 · 从场景起点计分钟',table(['车辆','工序','产线','准备开始','加工开始','加工完成','释放设备'],[...nodes.values()].map(x=>[x.vin,stage[x.stage]||x.stage,x.resource,x.B??'—',x.S??'—',x.C??'—',x.F??'—'].map(esc))))+
 details('未完成订单 · '+unmeta.length+'（未排'+unmeta.filter(x=>x.kind==='未排入').length+'台，已排未完成'+unmeta.filter(x=>x.kind!=='未排入').length+'台）',unmeta.length?table(['车辆','订单号','配置','状态','原因'],unmeta.map(x=>[esc(typeof x==='string'?x:x.vin),esc(x.order_id??'—'),esc(x.config??'—'),esc(x.kind||'—'),esc(x.reason||'—')]))+'<p class="muted">按有效需求（净额后订单,已剔除取消且未开工）减去真正完成集合判定;未排入与已排未完成都会阻断提交,系统不编造具体缺料归因。</p>':table(['车辆'],[]))+
 details('校验项目',table(['项目','状态'],(c.validation?.checks||[]).map(x=>[esc(x.rule),x.passed?'通过':'未通过'])))+
 details('全部计算指标',objectTable(Object.entries(c.metrics||{}).map(([k,v])=>({'指标':k,'数值':typeof v==='object'?JSON.stringify(v):v}))));
}
function renderCandidate(){const c=plan.result.candidates[selected];$('#candidate').innerHTML=`<h3>${esc(strategies[selected]||selected)}</h3>`+resultBody(c);
 document.querySelectorAll('[data-act="candidate"]').forEach(b=>b.classList.toggle('primary',b.dataset.key===selected));
 if($('#submit-action'))$('#submit-action').innerHTML=btn('提交所选方案','submit','','primary',!c.submittable||plan.status!=='ready')+(!c.submittable?note('不可提交：'+(c.submit_blockers||[]).join('；'),'bad'):'');}
function masterTables(data,editable=false){
 const input=(key,i,prop,value,label,type='number')=>editable?`<input aria-label="${esc(label)}" data-master="${key}.${i}.${prop}" type="${type}" value="${esc(value)}" ${type==='number'?'step="1"':''}>`:esc(value??'待完善');
 return details('产线资源',table(['编号','工序','状态'],data.resources.map((r,i)=>[input('resources',i,'id',r.id,`产线${i+1}编号`,'text'),editable?`<select aria-label="产线${i+1}工序" data-master="resources.${i}.stage">${Object.entries(stage).map(([k,v])=>`<option value="${k}" ${r.stage===k?'selected':''}>${v}</option>`).join('')}</select>`:esc(stage[r.stage]),editable?`<select aria-label="产线${i+1}状态" data-master="resources.${i}.enabled"><option value="true" ${r.enabled?'selected':''}>启用</option><option value="false" ${!r.enabled?'selected':''}>停用</option></select>`:r.enabled?'启用':'停用']))+(editable?btn('新增产线','add-resource'):'')+note('当前模型每个工序仅支持1条启用产线、单资源加工。新增产线可先停用，替换时先停用旧线；不提供无效的并行能力输入。'),true)+
 details('生产节拍（每车分钟）',table(['配置','车型','颜色','工序','节拍（分钟）'],data.times.map((t,i)=>[esc(t.config),esc(t.model),esc(t.color),esc(stage[t.stage]),input('times',i,'minutes',t.minutes,`${t.config} ${stage[t.stage]}节拍`)])),true)+
 details('生产日历',table(['班次','第几天','当日开始分钟','当日结束分钟'],data.shifts.map((s,i)=>[esc(s.id),input('shifts',i,'day',s.day,`班次${i+1}日期`),input('shifts',i,'start',s.start,`班次${i+1}开始`),input('shifts',i,'end',s.end,`班次${i+1}结束`)])))+
 details('工序衔接',table(['缓冲区','起点 → 终点','缓冲容量（辆）','固定转运（分钟）'],data.buffers.map((b,i)=>[esc(b.id),esc(b.edge),input('buffers',i,'capacity',b.capacity,`${b.id}缓冲容量`),input('buffers',i,'minutes',b.minutes,`${b.id}转运分钟`)])))+
 details('切换规则（演示假设）',table(['配置','工序','前值 → 后值','准备分钟'],data.setups.map((s,i)=>[esc(s.config),esc(stage[s.stage]),esc(s.pair),input('setups',i,'minutes',s.minutes,`${s.config} ${s.stage} ${s.pair}切换分钟`)])));
}
function renderInput(data){$('#view').innerHTML=`<div class="page-head"><div><h1>本次输入 · ${esc(plan.name)}</h1><p>演示输入快照 · 基础资料版本 ${esc(plan.input.master_revision)} · 本页只读，不是排程填写表单。</p></div>${link('返回排程',`#/plan/${plan.id}`)}</div>`+
 note('时间基准：从场景第1天00:00起累计分钟，不映射真实生产日期。人员、切换与转运包含演示假设。未来事件仅展示来源；当前计算以起点已知信息为基础，不宣称完成事件驱动重排。','warn')+
 card('需求与供应',details('订单 · '+data.orders.length+' 辆',objectTable(data.orders),true)+details('初始库存',objectTable(data.stock))+details('到货计划',objectTable(data.deliveries))+details('来源事件',objectTable(data.events))+details('人员资源（演示假设）',objectTable(data.labor)))+
 card('引用的基础资料',data.master?masterTables(data.master):note('历史输入没有版本化基础资料'));
}
let draft=null;
function renderMasters(){
 if(!catalogs.some(c=>c.id===catalogId))catalogId=catalogs[0]?.id||'';
 const c=catalogs.find(c=>c.id===catalogId);draft=c?structuredClone(c.data):null;const editable=me.role==='engineer';
 $('#view').innerHTML=`<div class="page-head"><div><h1>基础数据</h1><p>工艺负责人维护，计划引用版本；保存不会悄悄覆盖历史计划。</p></div>${returnPlan?link('返回原计划',`#/plan/${returnPlan}`):''}</div>`+
 note(editable?'当前可维护演示基础资料。真实企业参数须另行确认，不能直接用于生产。':'当前为只读查看。需要修改时请由工艺负责人维护，再回到计划引用最新版本。')+
 (c?`<label class="field">资料范围<select id="catalog-select">${catalogs.map(x=>`<option value="${esc(x.id)}" ${x.id===catalogId?'selected':''}>${esc(x.factory+' · '+x.name)}</option>`).join('')}</select></label>`+
 card('版本 '+c.revision+' · '+c.factory,`<p>来源：显式加载的演示场景。更新人：${esc(users[c.updated_by]||c.updated_by)} · ${esc(c.updated_at)}</p><div id="master-feedback" role="status">${c.checks?.length?note(c.checks.join('；'),'warn'):note('资料完整，可供排程引用。','ok')}</div><div id="master-fields">${masterTables(draft,editable)}</div>${editable?`<div class="sticky-actions">${btn('保存新版本','save-master','','primary')}${btn('放弃修改','discard-master')}</div>`:''}`):card('尚无基础资料','<div class="empty">没有预置数据。计划员在新建排程中选择演示场景后，相应演示基础资料才会出现。</div>'));
 $('#catalog-select')?.addEventListener('change',e=>{catalogId=e.target.value;renderMasters();});
}
function readMaster(){document.querySelectorAll('[data-master]').forEach(el=>{const [key,index,prop]=el.dataset.master.split('.');draft[key][index][prop]=el.type==='number'?(el.value===''?null:Number(el.value)):prop==='enabled'?el.value==='true':el.value;});}
function renderSystem(){$('#view').innerHTML=card('系统说明',note('本机演示服务已连接。当前管理员没有业务排程、修改或审批权限。')+'<p>未接入ERP / MES，不提供生产指令下发。页面角色切换仅用于演示，不是生产身份认证。</p>');}
async function choose(){if(!scenarios.length)scenarios=(await api('/preset-scenarios')).scenarios;
 modal('当前为演示数据，请选择你要测试的场景',note('选择后自动准备全部输入，不需要后续重复导入。更换场景将清除本计划当前计算结果，历史任务保留。','warn')+`<div class="scenario-grid">${scenarios.map((s,i)=>btn(({case_00_micro_benchmark_12:'正常生产 · 12辆基准',case_01_normal_baseline:'混流生产 · 产能压力',case_02_supply_delay_stress:'关键物料延期',case_03_material_shortage:'关键物料短缺',case_04_rush_tight_due:'紧急订单事件',case_05_order_cancellation:'订单撤销事件',adv_scenario_16_bottleneck_color_alum:'颜色与节拍瓶颈'})[s.id]||s.name,'load-scenario',`data-id="${esc(s.id)}"`)).join('')}</div>`+`<div class="scenario-grid" style="margin-top:14px">${btn('生产量级周计划 · 4200辆','production-scenario','','primary')}</div>`+btn('取消选择','close'));}
async function poll(id,token){try{const data=await api('/tasks/'+id);if(token!==epoch)return;task=data;
 if($('#progress'))$('#progress').innerHTML=note(data.stage||'计算中')+`<ol>${(data.progress_log||[]).map(x=>`<li>${esc(typeof x==='string'?x:x.stage||x.message||JSON.stringify(x))}</li>`).join('')}</ol>`;
 if(['completed','failed','done'].includes(data.status)){await refresh();return;}timer=setTimeout(()=>poll(id,token),700);
 }catch(e){if(token===epoch)notify(e.message,true);}}
async function action(act,el){
 if(busy)return;busy=true;el.disabled=true;
 try{
 switch(act){
 case 'refresh':await refresh();break;
 case 'close':closeModal();break;
 case 'new':modal('新建排程',`<form id="new-form">${field('计划名称','name','', 'text','required maxlength="100" placeholder="例如：验收-正常生产-01"')}<label class="field">工厂<select name="factory">${me.factories.filter(x=>/^F\d+$/.test(x)).map(x=>`<option>${esc(x)}</option>`).join('')}</select></label><p>计划类型：W+3演示排程。长期计划尚未开放。</p><div class="actions"><button class="btn primary" type="submit">下一步</button>${btn('取消','close')}</div></form>`);break;
 case 'choose':await choose();break;
 case 'load-scenario':await api('/plans/'+plan.id+'/load-xml-scenario','POST',{scenario_id:el.dataset.id});closeModal();await refresh();notify('演示输入已准备，后续步骤自动引用。');break;
 case 'to-master':returnPlan=plan.id;catalogId=plan.input?.master_id||'';location.hash='#/rules/'+encodeURIComponent(catalogId);break;
case 'production-scenario':if(!plan){notify('请先创建计划再选择场景。',true);break;}await api('/plans/'+plan.id+'/load-production-scenario','POST',{});closeModal();await refresh();notify('已在本计划加载生产量级周计划场景（4200辆）。');break;
 case 'refresh-master':modal('引用最新基础数据',note('当前结果将失效，需要重新生成方案。旧任务保留。','warn')+btn('确认引用并清除当前结果','confirm-master','','primary')+btn('取消','close'));break;
 case 'confirm-master':await api('/plans/'+plan.id+'/refresh-master','POST',{});closeModal();await refresh();notify('已引用最新版本，请检查数据后重新计算。');break;
 case 'save-master':{readMaster();const c=catalogs.find(x=>x.id===catalogId);const data=await api('/catalogs/'+encodeURIComponent(c.id),'PATCH',{revision:c.revision,data:draft});catalogs=catalogs.map(x=>x.id===c.id?data.catalog:x);renderMasters();notify('已保存基础资料版本 '+data.catalog.revision+'。现有计划需显式引用。');break;}
 case 'discard-master':renderMasters();notify('已恢复当前已保存版本。');break;
 case 'add-resource':readMaster();draft.resources.push({id:'',stage:'W',enabled:false});$('#master-fields').innerHTML=masterTables(draft,true);break;
 case 'run':{const data=await api('/plans/'+plan.id+'/run','POST',{});plan.status='running';renderPlan();await poll(data.task_id,epoch);break;}
 case 'candidate':selected=el.dataset.key;renderCandidate();break;
 case 'submit':modal('确认提交所选方案',`<p>计划：${esc(plan.name)}</p><p>方案：${esc(strategies[selected])}</p>`+note('提交后输入和计算将锁定。仅提交本机演示审批，不产生生产指令。','warn')+btn('确认提交','confirm-submit','','primary')+btn('取消','close'));break;
 case 'confirm-submit':await api('/plans/'+plan.id+'/submit','POST',{candidate:selected});closeModal();await refresh();notify('已提交主管审批。');break;
 case 'approve':modal('批准演示方案',note('仅批准当前选定演示方案，不向MES下发。')+btn('确认批准','confirm-approve','','primary')+btn('取消','close'));break;
 case 'confirm-approve':await api('/plans/'+plan.id+'/approve','POST',{});closeModal();await refresh();notify('已批准，演示记录已保留。');break;
 case 'history':{const t=await api('/tasks/'+el.dataset.id);modal('历史计算 · '+t.id,`<p>状态：${esc(t.status)} · ${esc(t.created_at)}</p>`+note('历史结果只读，不覆盖当前计划。')+(t.error?note(t.error,'bad'):'')+(t.result?Object.entries(t.result.candidates||{}).map(([k,c])=>details(strategies[k]||k,resultBody(c))).join(''):'<p>尚无结果</p>')+btn('关闭','close'));break;}
 }
 }catch(e){if(!$('#modal').hidden)$('#dialog-error').innerHTML=note(e.message,'bad');else if(act==='save-master')$('#master-feedback').innerHTML=note(e.message,'bad');notify(e.message,true);}
 finally{busy=false;if(el.isConnected)el.disabled=false;}
}
document.addEventListener('click',e=>{const el=e.target.closest('[data-act]');if(el&&!el.disabled)action(el.dataset.act,el);});
document.addEventListener('submit',async e=>{if(e.target.id!=='new-form')return;e.preventDefault();if(busy)return;busy=true;const submit=e.target.querySelector('[type="submit"]');submit.disabled=true;try{const f=new FormData(e.target);const d=await api('/plans','POST',{name:f.get('name'),factory:f.get('factory'),type:'W+3'});plan=d.plan;closeModal();history.replaceState(null,'','#/plan/'+plan.id);await refresh();await choose();}catch(err){$('#dialog-error').innerHTML=note(err.message,'bad');}finally{busy=false;submit.disabled=false;}});
document.addEventListener('keydown',e=>{if($('#modal').hidden)return;if(e.key==='Escape'&&!busy){closeModal();return;}if(e.key==='Tab'){const els=[...$('#modal').querySelectorAll('button:not(:disabled),input,select,a[href]')];if(!els.length)return;const first=els[0],last=els[els.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});
$('#userSelect').addEventListener('change',async e=>{user=e.target.value;localStorage.setItem('aps.user',user);catalogId='';returnPlan='';closeModal();history.replaceState(null,'',user==='admin'?'#/system':'#/plans');await refresh();});
$('#reloadBtn').addEventListener('click',refresh);window.addEventListener('hashchange',refresh);refresh();
})();
