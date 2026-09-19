'use strict';

/* =============================================================================
 * APS 计划与排程平台 · 前端应用
 * -----------------------------------------------------------------------------
 * 零依赖原生 JS。通过 fetch 调用本机后端 API，模拟身份用 X-Demo-User 请求头传递。
 *
 * 边界声明（与产品一致，界面同样如实呈现）：
 *  1. 演示环境，未接入 ERP / MES；提交与批准不产生任何生产指令。
 *  2. 仅 W+3 周计划接入后端真实计算；N+1 / N+3 / N+6 的计算未实现，
 *     界面明确标注，不会复用 W+3 算法生成结果来伪装。
 *  3. 所有结果指标一律来自后端返回并经过 normalizeResult() 归一化，
 *     归一化失败时只展示原始 JSON 与接入说明，绝不臆造数值。
 *  4. 服务端负责数据权限；前端按角色显示菜单与操作入口，不构成生产鉴权。
 * ============================================================================= */

/* ============================ 1. 常量配置 ============================ */

const USERS = [
  { user: 'planner-a', label: '计划员 A · F1' },
  { user: 'planner-b', label: '计划员 B · F2' },
  { user: 'manager', label: '生产主管 · F1+F2' },
  { user: 'engineer', label: '工艺工程师 · F1 只读 + 规则' },
  { user: 'admin', label: '系统管理员 · 无审批权' }
];

const PLAN_TYPES = [
  { key: 'W+3', label: 'W+3 周计划', computable: true, note: '已接入后端真实计算，可运行并查看结果。' },
  { key: 'N+1', label: 'N+1 月计划', computable: false, note: '月计划计算尚未实现，界面不会套用 W+3 算法生成结果。' },
  { key: 'N+3', label: 'N+3 中期预测', computable: false, note: '中期预测计算尚未实现，界面不会套用 W+3 算法生成结果。' },
  { key: 'N+6', label: 'N+6 长期预测', computable: false, note: '长期预测计算尚未实现，界面不会套用 W+3 算法生成结果。' }
];

const TYPE_LABEL = PLAN_TYPES.reduce(function (m, t) { m[t.key] = t.label; return m; }, {});
const TYPE_MAP = PLAN_TYPES.reduce(function (m, t) { m[t.key] = t; return m; }, {});

/* 计划状态：draft / running / ready / failed / pending / approved（后端约定）
   任务状态：queued / running / completed / failed */
const STATUS_LABEL = {
  draft: '草稿', created: '已创建', new: '已创建', input_ready: '输入就绪',
  ready: '计算就绪', queued: '排队中', running: '计算中', computing: '计算中',
  computed: '已计算', completed: '已完成', failed: '失败',
  pending: '待审批', pending_approval: '待审批', submitted: '待审批', approving: '待审批',
  approved: '已批准', rejected: '已驳回', archived: '已归档'
};

const MENU_LABEL = { plans: '计划管理', inbox: '待办审批', todos: '待办审批', rules: '规则资源', system: '系统' };

const STEP_NAMES = ['基本信息', '接口输入', '约束目标', '计算仿真', '方案分析', '确认提交'];

const API_PATHS = {
  me: '/api/me',
  plans: '/api/plans',
  sample: '/api/sample',
  system: '/api/system',
  plan: function (id) { return '/api/plans/' + encodeURIComponent(id); },
  run: function (id) { return '/api/plans/' + encodeURIComponent(id) + '/run'; },
  submit: function (id) { return '/api/plans/' + encodeURIComponent(id) + '/submit'; },
  approve: function (id) { return '/api/plans/' + encodeURIComponent(id) + '/approve'; },
  task: function (id) { return '/api/tasks/' + encodeURIComponent(id); }
};

const POLL_INTERVAL_MS = 2000;

/* ============================ 2. 基础工具 ============================ */

function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function escAttr(v) { return esc(v); }

function safeJson(v) {
  try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
}

function fmtTime(v) {
  if (!v) return '—';
  const d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  const p = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function nowStamp() {
  const d = new Date();
  const p = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function setPath(obj, path, val) {
  const parts = String(path).split('.');
  let o = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (!o[parts[i]] || typeof o[parts[i]] !== 'object') o[parts[i]] = {};
    o = o[parts[i]];
  }
  o[parts[parts.length - 1]] = val;
}

function getPath(obj, path) {
  return String(path).split('.').reduce(function (o, k) { return (o === null || o === undefined) ? undefined : o[k]; }, obj);
}

let toastTimer = null;
function toast(msg, tone) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast' + (tone ? ' ' + tone : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.hidden = true; }, 3800);
}

function tagHtml(text, tone) {
  return '<span class="tag' + (tone ? ' ' + tone : '') + '">' + esc(text) + '</span>';
}

function typeLabel(t) {
  if (!t) return '—';
  return TYPE_LABEL[t] || String(t);
}

function statusLabel(s) {
  if (!s) return '—';
  const key = String(s).toLowerCase();
  return STATUS_LABEL[key] || String(s);
}

function statusTone(s) {
  const k = String(s || '').toLowerCase();
  if (/fail|reject|error|cancel|无解/.test(k)) return 'bad';
  if (/run|queu|comput/.test(k)) return 'warn';
  if (/pending|approv|submitted/.test(k)) return 'warn';
  if (/approved|completed|done|success/.test(k)) return 'ok';
  if (/ready/.test(k)) return 'cyan';
  if (/draft|created|new/.test(k)) return 'plain';
  return 'info';
}

function isComputable(type) {
  const t = TYPE_MAP[type];
  return !!(t && t.computable);
}

function typeNote(type) {
  const t = TYPE_MAP[type];
  return t ? t.note : '未知计划类型：后端未声明其计算能力，界面不会假定可计算。';
}

/* ============================ 3. API 层 ============================ */

function ApiError(message, status, data) {
  this.name = 'ApiError';
  this.message = message || '请求失败';
  this.status = status || 0;
  this.data = data || null;
}
ApiError.prototype = Object.create(Error.prototype);

function apiBase() {
  try { return localStorage.getItem('aps.apiBase') || ''; } catch (e) { return ''; }
}

function currentUser() {
  try { return localStorage.getItem('aps.user') || 'planner-a'; } catch (e) { return 'planner-a'; }
}

function setCurrentUser(u) {
  try { localStorage.setItem('aps.user', u); } catch (e) { /* 忽略存储异常 */ }
}

async function req(method, path, body) {
  const url = apiBase() + path;
  const headers = { 'Accept': 'application/json' };
  headers['X-Demo-User'] = currentUser();
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  let res;
  try {
    res = await fetch(url, {
      method: method,
      headers: headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch (e) {
    throw new ApiError('无法连接后端服务（' + (apiBase() || '同源') + '）：' + (e && e.message ? e.message : '网络错误'), 0);
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
  }

  if (!res.ok) {
    let msg = null;
    if (data && typeof data === 'object') msg = data.error || data.message || data.detail || data.msg;
    if (!msg && data && data.raw) msg = String(data.raw).slice(0, 200);
    if (!msg) msg = 'HTTP ' + res.status;
    if (res.status === 401) msg = '身份未被服务端接受（401）：' + msg;
    if (res.status === 403) msg = '当前身份无权执行该操作（403）：' + msg;
    throw new ApiError(msg, res.status, data);
  }
  return data;
}

const api = {
  me: function () { return req('GET', API_PATHS.me); },
  plans: function () { return req('GET', API_PATHS.plans); },
  plan: function (id) { return req('GET', API_PATHS.plan(id)); },
  createPlan: function (payload) { return req('POST', API_PATHS.plans, payload); },
  patchPlan: function (id, payload) { return req('PATCH', API_PATHS.plan(id), payload); },
  sample: function () { return req('GET', API_PATHS.sample); },
  system: function () { return req('GET', API_PATHS.system); },
  run: function (id) { return req('POST', API_PATHS.run(id), {}); },
  task: function (id) { return req('GET', API_PATHS.task(id)); },
  submit: function (id, candidate) { return req('POST', API_PATHS.submit(id), { candidate: candidate }); },
  approve: function (id) { return req('POST', API_PATHS.approve(id), {}); },
  presetScenarios: function () { return req('GET', '/api/preset-scenarios'); },
  presetScenarioXml: function (scId) { return req('GET', '/api/preset-scenarios/' + scId + '/xml'); },
  loadXmlScenario: function (planId, scId) { return req('POST', '/api/plans/' + planId + '/load-xml-scenario', { scenario_id: scId }); },
  getDbRecords: function (planId) { return req('GET', '/api/plans/' + planId + '/db-records'); }
};

/* ============================ 4. 全局状态 ============================ */

const state = {
  route: { view: 'plans', id: null, step: 1 },

  me: null,
  meError: null,
  backendOk: null,          // null 未知 / true 连通 / false 失败

  plans: [],
  plansLoading: false,
  plansError: null,
  details: {},              // planId -> {loading, error, plan}
  expanded: null,

  search: '',
  filterType: 'all',

  plan: null,
  planLoading: false,
  planError: null,

  sample: null,
  sampleError: null,

  system: null,
  systemError: null,

  newPlan: { name: '', type: 'W+3', factory: '' },

  editor: { inputText: '', inputError: null, dirty: false },

  /* 步骤 3 只维护引擎真正解析的字段（见 ENGINE_INPUT_SCHEMA），
     不再提供会被静默忽略的自由 JSON。 */
  schema: null,
  processModelText: {},

  taskId: null,
  taskDetail: null,
  taskError: null,
  taskTimer: null,
  pendingTaskId: null,

  rawResult: null,
  result: null,
  selectedCandidate: null,

  assistantLog: [],

  confirm: null,          // 页内二次确认（不使用原生 confirm，避免阻塞自动化）

  actionBusy: null,
  presetScenarios: null,
  xmlPreviewOpen: false,
  dbRecords: null,
  dbRecordsOpen: false
};

/* ============================ 5. 角色与权限（前端显示层） ============================ */

function roleKind() {
  const r = String((state.me && state.me.role) || '').toLowerCase();
  if (r.indexOf('manager') >= 0 || r.indexOf('主管') >= 0) return 'manager';
  if (r.indexOf('engineer') >= 0 || r.indexOf('工艺') >= 0) return 'engineer';
  if (r.indexOf('admin') >= 0 || r.indexOf('管理') >= 0) return 'admin';
  if (r.indexOf('planner') >= 0 || r.indexOf('计划员') >= 0) return 'planner';
  return 'viewer';
}

function canEditPlanInput() { return roleKind() === 'planner'; }
function canRunCompute() { return roleKind() === 'planner'; }
function canSubmitPlan() { return roleKind() === 'planner'; }
function canApprovePlan() { return roleKind() === 'manager'; }

function statusLocked() {
  const s = String((state.plan && state.plan.status) || '').toLowerCase();
  if (!s) return false;
  return /run|submitted|pending|approv|归档|archiv/.test(s);
}

function statusLockReason() {
  const s = state.plan && state.plan.status;
  if (!s) return '';
  if (/run/i.test(s)) return '计划正在计算中，输入已锁定。';
  if (/submitted|pending|approv/i.test(s)) return '计划已提交或进入审批，内容已冻结，不能静默修改。';
  if (/archiv/i.test(s)) return '计划已归档，只读。';
  return '';
}

function defaultMenus() {
  const kind = roleKind();
  const menus = [{ key: 'plans', label: MENU_LABEL.plans }];
  if (kind === 'manager') menus.push({ key: 'inbox', label: MENU_LABEL.inbox });
  if (kind === 'engineer') menus.push({ key: 'rules', label: MENU_LABEL.rules });
  if (kind === 'admin') menus.push({ key: 'system', label: MENU_LABEL.system });
  return menus;
}

function resolveMenus() {
  const me = state.me;
  if (me && Array.isArray(me.menus) && me.menus.length) {
    return me.menus.map(function (m) {
      if (typeof m === 'string') return { key: m, label: MENU_LABEL[m] || m };
      const key = m.key || m.id || m.name;
      return { key: key, label: m.label || m.title || MENU_LABEL[key] || key };
    }).filter(function (m) { return m.key; });
  }
  return defaultMenus();
}

function firstAllowedMenu() {
  const menus = resolveMenus();
  return menus.length ? menus[0].key : 'plans';
}

/** 服务端下发的菜单决定可见视图；工作空间由计划列表进入，不单独占菜单。 */
function viewAllowed(view) {
  if (view === 'workspace') return true;
  return resolveMenus().some(function (m) { return m.key === view; });
}

/* ============================ 6. 路由 ============================ */

function parseHash() {
  const raw = String(location.hash || '').replace(/^#\/?/, '');
  const parts = raw.split('/').filter(Boolean);
  if (!parts.length) return { view: 'plans', id: null, step: 1 };
  if (parts[0] === 'plan' && parts[1]) {
    const step = Math.min(6, Math.max(1, parseInt(parts[2], 10) || 1));
    return { view: 'workspace', id: parts[1], step: step };
  }
  const view = parts[0] === 'todos' ? 'inbox' : parts[0];
  return { view: view, id: null, step: 1 };
}

function go(hash) {
  if (location.hash === hash) { handleRoute(); return; }
  location.hash = hash;
}

/* ============================ 7. 数据加载 ============================ */

async function loadMe() {
  state.meError = null;
  try {
    const d = await api.me();
    state.me = d && d.user !== undefined ? d : (d || null);
    state.backendOk = true;
  } catch (e) {
    state.me = null;
    state.meError = e.message || '无法获取当前身份';
    state.backendOk = e.status === 0 ? false : state.backendOk;
  }
}

async function loadPlans() {
  state.plansLoading = true;
  state.plansError = null;
  render();
  try {
    const d = await api.plans();
    const list = d && Array.isArray(d.plans) ? d.plans : (Array.isArray(d) ? d : []);
    state.plans = list;
    state.backendOk = true;
  } catch (e) {
    state.plansError = e.message || '计划列表加载失败';
    if (e.status === 0) state.backendOk = false;
  }
  state.plansLoading = false;
  render();
}

async function loadPlanDetail(id, force) {
  const key = String(id);
  if (!force && state.details[key] && state.details[key].plan) return state.details[key].plan;
  state.details[key] = { loading: true, error: null, plan: null };
  render();
  try {
    const d = await api.plan(id);
    const plan = d && d.plan ? d.plan : d;
    state.details[key] = { loading: false, error: null, plan: plan };
  } catch (e) {
    state.details[key] = { loading: false, error: e.message || '计划详情加载失败', plan: null };
  }
  render();
  return state.details[key].plan;
}

function resetWorkspace() {
  stopPoll();
  state.plan = null;
  state.planId = null;
  state.planError = null;
  state.taskId = null;
  state.taskDetail = null;
  state.taskError = null;
  state.pendingTaskId = null;
  state.rawResult = null;
  state.result = null;
  state.selectedCandidate = null;
  state.assistantLog = [];
  state.editor = { inputText: '', inputError: null, dirty: false };
  state.schema = null;
  state.processModelText = {};
}

async function loadWorkspace(id) {
  if (id === 'new') { resetWorkspace(); render(); return; }
  if (state.plan && String(state.plan.id) === String(id)) return;

  resetWorkspace();
  state.planLoading = true;
  state.route.id = String(id);
  render();

  try {
    const d = await api.plan(id);
    state.plan = d && d.plan ? d.plan : d;
    state.planId = state.plan && state.plan.id !== undefined ? String(state.plan.id) : String(id);
    hydrateEditorFromPlan();
    state.selectedCandidate = state.plan.selected_candidate || null;
    if (state.plan.result) {
      state.rawResult = state.plan.result;
      state.result = normalizeResult(state.plan.result, state.plan);
    }
    pickDefaultCandidate();
    resumeLatestTask();
    state.backendOk = true;
  } catch (e) {
    state.planError = e.message || '计划加载失败';
    if (e.status === 0) state.backendOk = false;
  }
  state.planLoading = false;
  render();
}

function hydrateEditorFromPlan() {
  const input = state.plan && state.plan.input;
  state.editor.inputText = input ? safeJson(input) : '';
  state.editor.inputError = null;
  state.editor.dirty = false;
  hydrateSchema();
}

/** 深拷贝一份引擎输入，供步骤 3 的结构化控件编辑。 */
function hydrateSchema() {
  const input = state.plan && state.plan.input;
  state.schema = input ? JSON.parse(JSON.stringify(input)) : null;
  state.processModelText = {};
  const pt = state.schema && state.schema.process_times;
  if (pt && typeof pt === 'object') {
    Object.keys(pt).forEach(function (stage) {
      const bm = pt[stage] && pt[stage].by_model;
      state.processModelText[stage] = bm ? JSON.stringify(bm) : '';
    });
  }
}

async function loadSample() {
  state.sampleError = null;
  try {
    state.sample = await api.sample();
    state.backendOk = true;
  } catch (e) {
    state.sample = null;
    state.sampleError = e.message || '样例数据加载失败';
    if (e.status === 0) state.backendOk = false;
  }
  render();
}

async function loadPresetScenarios() {
  let loaded = false;
  try {
    const d = await api.presetScenarios();
    state.presetScenarios = d && Array.isArray(d.scenarios) ? d.scenarios : [];
    loaded = true;
  } catch (e) {
    state.presetScenarios = [];
    toast('典型场景库加载失败：' + (e.message || '网络异常'), 'bad');
  }
  /* 启动流程不 await 该请求：数据到达后补一次渲染，避免第 2 步场景卡片区域空白 */
  if (loaded) render();
}

function renderDbRecordsModal() {
  const modal = document.getElementById('modal');
  if (!modal || !state.dbRecords) return;

  const d = state.dbRecords;
  const hdr = d.header_table && d.header_table[0];

  const candRows = (d.candidates_table || []).map(function (c) {
    return '<tr>' +
      '<td>' + esc(c.strategy_name) + '</td>' +
      '<td>' + esc(c.is_valid ? '是' : '否') + '</td>' +
      '<td>' + esc(c.unmet_count) + '</td>' +
      '<td>' + esc(c.true_weighted_tardiness_min) + '</td>' +
      '<td>' + esc(c.total_blocked_min) + 'm</td>' +
      '<td>' + esc(c.color_switches) + '</td>' +
      '<td>' + esc(c.makespan_min) + 'm</td>' +
      '<td>' + esc(c.is_selected ? '是' : '') + '</td>' +
    '</tr>';
  }).join('');

  const detailRows = (d.details_table_sample || []).slice(0, 20).map(function (r) {
    return '<tr>' +
      '<td>' + esc(r.vin) + '</td>' +
      '<td>' + esc(r.stage) + '</td>' +
      '<td>' + esc(r.node_b_min) + 'm</td>' +
      '<td>' + esc(r.node_s_min) + 'm</td>' +
      '<td>' + esc(r.node_c_min) + 'm</td>' +
      '<td>' + esc(r.node_f_min) + 'm</td>' +
      '<td>' + esc(r.blocked_duration_min) + 'm</td>' +
    '</tr>';
  }).join('');

  const orderRows = (d.orders_table_sample || []).slice(0, 15).map(function (r) {
    return '<tr>' +
      '<td>' + esc(r.vin) + '</td>' +
      '<td>' + esc(r.model_type) + '</td>' +
      '<td>' + esc(r.color) + '</td>' +
      '<td>' + esc(r.is_pullout ? '是' : '否') + '</td>' +
      '<td>' + esc(r.priority) + '</td>' +
      '<td>' + esc(r.due_at_min) + 'm</td>' +
    '</tr>';
  }).join('');

  modal.innerHTML = '<div class="modal-overlay">' +
    '<div class="modal-card" style="max-width: 1100px; max-height: 85vh; overflow-y:auto">' +
      '<div class="modal-head"><h2>数据库关系表记录核验</h2><button class="btn small" data-act="closeDbRecords">关闭</button></div>' +
      '<div class="modal-body">' +
        '<div class="note warn">以下内容直接查询后端 SQLite 关系表 (aps_plan_header, aps_plan_candidate, aps_plan_detail, aps_order_master, aps_material_ledger)，用于演示层物理入库核验。</div>' +

        '<div class="card" style="margin-top:16px"><div class="card-head"><h3>1. 计划主表 (aps_plan_header)</h3></div>' +
          '<div class="card-body">' + (hdr ? '<div class="statline">' +
            statItem('计划ID', hdr.plan_id, true) +
            statItem('计划类型', hdr.plan_type, true) +
            statItem('工厂', hdr.plant_code, true) +
            statItem('状态', hdr.status, true) +
            statItem('选定算法', hdr.selected_candidate_id, true) +
            statItem('版本', hdr.version, true) +
          '</div>' : '<div class="empty">暂无记录</div>') + '</div></div>' +

        '<div class="card" style="margin-top:12px"><div class="card-head"><h3>2. 算法候选方案表 (aps_plan_candidate)</h3></div>' +
          '<div class="card-body"><table class="table"><thead><tr><th>算法策略</th><th>合规</th><th>未排</th><th>拖期</th><th>阻塞</th><th>换色</th><th>完工期</th><th>选定</th></tr></thead><tbody>' +
            candRows + '</tbody></table></div></div>' +

        '<div class="card" style="margin-top:12px"><div class="card-head"><h3>3. 逐车排程四节点明细表 (aps_plan_detail) [抽样前20行]</h3></div>' +
          '<div class="card-body"><table class="table"><thead><tr><th>VIN</th><th>阶段</th><th>准备开始(B)</th><th>加工开始(S)</th><th>完工(C)</th><th>释放(F)</th><th>阻塞(F-C)</th></tr></thead><tbody>' +
            detailRows + '</tbody></table></div></div>' +

        '<div class="card" style="margin-top:12px"><div class="card-head"><h3>4. 订单主数据表 (aps_order_master) [抽样前15行]</h3></div>' +
          '<div class="card-body"><table class="table"><thead><tr><th>VIN</th><th>车型</th><th>颜色</th><th>拔出</th><th>优先级</th><th>交期(分)</th></tr></thead><tbody>' +
            orderRows + '</tbody></table></div></div>' +
      '</div>' +
    '</div>' +
  '</div>';
  modal.hidden = false;
}

async function loadSystem() {
  state.systemError = null;
  try {
    state.system = await api.system();
    state.backendOk = true;
  } catch (e) {
    state.system = null;
    state.systemError = e.message || '系统信息加载失败';
    if (e.status === 0) state.backendOk = false;
  }
}

/* ============================ 8. 计算任务与轮询 ============================ */

function taskIsRunning(t) {
  if (!t) return false;
  if (t.result) return false;
  const s = String(t.status || '').toLowerCase();
  if (!s) return false;
  if (/fail|error|cancel|timeout|succeed|success|done|complet|finish|无解|失败|完成|取消/.test(s)) return false;
  return /queu|pend|run|start|execut|comput|schedul|排队|运行|计算|执行/.test(s);
}

function stopPoll() {
  if (state.taskTimer) { clearTimeout(state.taskTimer); state.taskTimer = null; }
}

function resumeLatestTask() {
  const tasks = (state.plan && Array.isArray(state.plan.tasks)) ? state.plan.tasks : [];
  if (!tasks.length) return;
  let lastId = null;
  try { lastId = localStorage.getItem('aps.lastTask.' + state.planId); } catch (e) { lastId = null; }
  const known = tasks.filter(function (t) { return String(t.id) === String(lastId); })[0];
  // 服务端按创建时间倒序返回任务，tasks[0] 即最近一次任务
  const latest = known || tasks[0];
  if (latest && latest.id !== undefined) {
    state.taskId = String(latest.id);
    state.taskDetail = { id: latest.id, status: latest.status, stage: latest.stage, created_at: latest.created_at };
    if (taskIsRunning(latest)) startPoll(state.taskId, true);
  }
}

function startPoll(taskId, silent) {
  stopPoll();
  const tick = async function () {
    let d = null;
    try {
      d = await api.task(taskId);
    } catch (e) {
      state.taskError = e.message || '任务状态查询失败';
      stopPoll();
      if (!silent) render();
      return;
    }
    applyTask(taskId, d);
    if (taskIsRunning(d)) {
      state.taskTimer = setTimeout(tick, POLL_INTERVAL_MS);
    } else {
      state.taskTimer = null;
      stopPoll();
      if (state.planId) await refreshPlanQuiet(state.planId);
      await refreshPlansQuiet();
      toast('计算任务已结束：' + statusLabel(d && d.status), d && d.error ? 'bad' : 'ok');
    }
    renderIfTaskView();
  };
  state.taskTimer = setTimeout(tick, 200);
}

function applyTask(taskId, d) {
  state.taskId = String(taskId);
  state.taskDetail = d || null;
  state.taskError = (d && d.error) ? d.error : null;
  const raw = d && d.result ? d.result : null;
  if (raw) {
    state.rawResult = raw;
    state.result = normalizeResult(raw, state.plan);
    pickDefaultCandidate();
  }
}

function renderIfTaskView() {
  if (state.route.view === 'workspace' && (state.route.step === 4 || state.route.step === 5)) render();
}

async function refreshPlansQuiet() {
  try {
    const d = await api.plans();
    state.plans = d && Array.isArray(d.plans) ? d.plans : state.plans;
    if (state.route.view === 'plans') render();
  } catch (e) { /* 静默：轮询刷新失败不打断界面 */ }
}

function pickDefaultCandidate() {
  const r = state.result;
  if (!r || !r.candidates || !r.candidates.length) return;
  const keys = r.candidates.map(function (c) { return c.key; });
  if (state.selectedCandidate && keys.indexOf(state.selectedCandidate) >= 0) return;
  if (state.plan && state.plan.selected_candidate && keys.indexOf(state.plan.selected_candidate) >= 0) {
    state.selectedCandidate = state.plan.selected_candidate;
    return;
  }
  if (keys.indexOf('optimized') >= 0) { state.selectedCandidate = 'optimized'; return; }
  state.selectedCandidate = keys[0];
}

/* =============================================================================
 * 9. normalizeResult —— 结果结构归一化（隔离点）
 * -----------------------------------------------------------------------------
 * 输入：后端 /api/tasks/{id}.result，即 engine/solve.py 中 solve() 的返回值。
 * 输出：界面渲染唯一依赖的结构。本函数绝不臆造数据：字段缺失就返回空数组或
 *       null，由界面显示“结果未提供”，而不是补默认值。
 *
 * 目标结构：
 * {
 *   candidates: [{
 *     key: 'baseline' | 'optimized',
 *     label, sublabel,
 *     metrics: [{ key, label, value, unit, fmt }],     // fmt: int|num|rate|''
 *     gantt: { resources, buckets, events } | null,
 *     validation: [{ item, level:'pass'|'fail', detail }],
 *     validationSummary: { passed, total, failed },
 *     unscheduled: [{ order, reason, detail, scope }],
 *     submittable, submitBlockers: [string]
 *   }],
 *   objectiveTiers: [{ tier, label, baseline, optimized, delta, improved }],
 *   meta: { engineVersion, seed, ..., ruleScope, caveats, warnings, runtime },
 *   explanation: { summary, basis }, raw
 * }
 * 返回 null 表示结构无法识别 —— 界面展示原始 JSON 并说明“结构待接入”，不做推断。
 * 引擎返回结构变化时只需修改本段，视图层不需要改动。
 * ============================================================================= */

const METRIC_DEFS = [
  ['total_cars', '车辆总数', '台', 'int'],
  ['scheduled_cars', '已排车辆', '台', 'int'],
  ['unscheduled_cars', '未排车辆', '台', 'int'],
  ['total_orders', '订单总数', '单', 'int'],
  ['scheduled_orders', '已排订单', '单', 'int'],
  ['unscheduled_orders', '完全未排订单', '单', 'int'],
  ['on_time_cars', '按时投产车辆（非完工）', '台', 'int'],
  ['late_cars', '延期投产车辆', '台', 'int'],
  ['tardiness_days', '延期天数合计', '天', 'int'],
  ['max_lateness_days', '最大延期', '天', 'int'],
  ['avg_lateness_days', '平均延期', '天', 'num'],
  ['fulfillment_rate', '需求满足率', '', 'rate'],
  ['on_time_rate', '准时率（全部）', '', 'rate'],
  ['on_time_rate_of_scheduled', '准时率（已排）', '', 'rate'],
  ['flow_on_time_cars', '流转准时车辆', '台', 'int'],
  ['flow_late_cars', '流转延期车辆', '台', 'int'],
  ['flow_span_days', '流转跨度', '天', 'int'],
  ['makespan_seconds', '总完工时长', '秒', 'int'],
  ['changeover_seconds_total', '切换总时长', '秒', 'int'],
  ['wait_seconds_total', '等待总时长', '秒', 'int'],
  ['wait_seconds_avg', '平均等待', '秒', 'num'],
  ['wait_seconds_max', '最大等待', '秒', 'num'],
  ['drain_days_used', '收尾天数', '天', 'int'],
  ['flow_backlog_cars', '流转积压车辆', '台', 'int'],
  ['days_planned', '有排产天数', '天', 'int'],
  ['capacity_welded_cars', '焊装上線车辆', '台', 'int']
];

const METRIC_DICT_DEFS = [
  ['changeover_seconds', '切换时长', '秒', 'num'],
  ['changeover_count', '切换次数', '次', 'int'],
  ['wait_seconds_by_stage', '等待时长', '秒', 'num'],
  ['utilization', '产能利用率', '', 'rate'],
  ['busy_seconds', '忙碌时长', '秒', 'num']
];

const TIER_LABELS = {
  unscheduled_cars: '未排车辆',
  late_cars: '延期车辆',
  tardiness_days: '延期天数合计',
  flow_span_days: '流转跨度（天）',
  changeover_seconds: '切换总时长（秒）',
  total_wait_seconds: '等待总时长（秒）'
};

const REASON_LABELS = {
  material: '物料供应不足',
  material_and_capacity: '物料与产能双重受限',
  weld_capacity: '焊装产能不足',
  paint_capacity: '涂装产能不足',
  assembly_capacity: '总装产能不足',
  day_car_limit: '超出日车辆上限',
  release_after_horizon: '投产日晚于计划期末',
  internal_inconsistency: '引擎内部不一致（异常，请反馈）',
  unknown: '未归类原因'
};

/* 集成引擎返回阶段码 W/P/A；旧引擎使用 weld/paint/assembly */
const STAGE_FALLBACK = {
  weld: '焊装', paint: '涂装', assembly: '总装',
  W: '焊装', P: '涂装', A: '总装'
};

function strategyText(s) {
  if (!s || typeof s !== 'object') return '';
  const parts = [];
  if (s.assign) parts.push('分配 ' + s.assign);
  if (s.sequence) parts.push('排序 ' + s.sequence);
  return parts.join(' · ');
}

function normalizeResult(raw, plan) {
  if (!raw || typeof raw !== 'object') return null;

  /* ---- 兼容 aps-fullchain 集成引擎返回的结构 ---- */
  if (raw.engine === 'aps_fullchain_v1_integrated') {
    const candidates = [];
    const candMap = raw.candidates || {};
    const keys = Object.keys(candMap);
    keys.forEach(function (key) {
      const p = candMap[key];
      const label = key === raw.selected_strategy ? '推荐方案 (选定)' : (key === 'Strategy_A_EDD' ? '基线方案 (EDD)' : key.replace('Strategy_', '').replace('_', ' '));
      candidates.push(normCandidate(key, label, p, normEngineMeta(raw)));
    });
    if (!candidates.length) return null;
    return {
      candidates: candidates,
      objectiveTiers: [],
      searchCandidates: [],
      meta: normEngineMeta(raw),
      explanation: normExplanationRaw(raw),
      raw: raw,
      fullchain: true,
      selected_strategy: raw.selected_strategy
    };
  }

  const base = (raw.baseline && typeof raw.baseline === 'object') ? raw.baseline : null;
  const opt = (raw.optimized && typeof raw.optimized === 'object') ? raw.optimized : null;
  if (!base && !opt) return null;

  const meta = normEngineMeta(raw);
  const candidates = [];
  if (base) candidates.push(normCandidate('baseline', '基线方案', base, meta));
  if (opt) candidates.push(normCandidate('optimized', '优化方案', opt, meta));
  if (!candidates.length) return null;

  return {
    candidates: candidates,
    objectiveTiers: normTiers(raw.comparison),
    searchCandidates: normSearchCandidates(raw.candidates),
    meta: meta,
    explanation: normExplanationRaw(raw),
    raw: raw
  };
}

function normSearchCandidates(list) {
  if (!Array.isArray(list)) return [];
  return list.map(function (c) {
    if (!c || typeof c !== 'object') return null;
    const obj = c.objective || {};
    return {
      name: String(c.name || '—'),
      strategy: strategyText(c.strategy),
      objective: Array.isArray(obj.lexicographic) ? obj.lexicographic.join(' > ') : '',
      weightedPenalty: obj.weighted_penalty,
      scheduledCars: c.scheduled_cars,
      unscheduledCars: c.unscheduled_cars,
      lateCars: c.late_cars,
      tardinessDays: c.tardiness_days,
      changeoverSeconds: c.changeover_seconds_total,
      flowSpanDays: c.flow_span_days,
      validationPassed: c.validation_passed === undefined ? null : !!c.validation_passed,
      submittable: !!c.submittable,
      blockers: Array.isArray(c.submit_blockers) ? c.submit_blockers.map(String) : []
    };
  }).filter(Boolean);
}

function normCandidate(key, label, p, meta) {
  const validation = normChecks(p.validation);
  const failed = validation.filter(function (v) { return v.level === 'fail'; }).length;
  return {
    key: key,
    label: label,
    sublabel: p.candidate_name ? String(p.candidate_name) : strategyText(p.strategy),
    metrics: normMetricsFromPlan(p.metrics),
    gantt: normGanttFromPlan(p, meta),
    validation: validation,
    validationSummary: {
      passed: !!(p.validation && p.validation.passed),
      total: validation.length,
      failed: failed
    },
    unscheduled: normUnscheduledList(p.unscheduled),
    dayLoads: normDayLoads(p),
    sequence: normSequenceRows(p),
    submittable: !!p.submittable,
    submitBlockers: Array.isArray(p.submit_blockers) ? p.submit_blockers.map(String) : []
  };
}

/** 分日负荷：直接读 schedule.days，不做任何换算以外的加工。 */
function normDayLoads(p) {
  const days = (p.schedule && Array.isArray(p.schedule.days)) ? p.schedule.days : null;
  if (!days) return [];
  return days.map(function (d) {
    const util = d.planned_utilization || {};
    const total = d.planned_total_seconds || {};
    const cap = d.capacity_seconds || {};
    return {
      day: d.day,
      isWorkday: d.is_workday !== false,
      assignedCars: d.assigned_cars,
      weldTotal: total.weld,
      paintTotal: total.paint,
      assemblyTotal: total.assembly,
      weldCapacity: cap.weld,
      weldUtil: util.weld,
      paintUtil: util.paint,
      assemblyUtil: util.assembly,
      carryIn: d.carry_in_model || ''
    };
  });
}

/** 逐车序位明细：字段来自 schedule.sequence；界面只展示前若干行。 */
function normSequenceRows(p) {
  const rows = (p.schedule && Array.isArray(p.schedule.sequence)) ? p.schedule.sequence : null;
  if (!rows) return [];
  return rows.map(function (r) {
    return {
      position: r.position,
      day: r.day,
      carId: r.car_id,
      orderId: r.order_id,
      model: r.model,
      color: r.color,
      releaseDay: r.release_day,
      dueDay: r.due_day,
      lateDays: r.lateness_days
    };
  });
}

function normMetricsFromPlan(m) {
  if (!m || typeof m !== 'object') return [];
  const out = [];
  const seen = {};
  const labels = {};
  METRIC_DEFS.forEach(function (d) {
    seen[d[0]] = 1;
    if (!Object.prototype.hasOwnProperty.call(m, d[0])) return;
    const v = m[d[0]];
    if (typeof v === 'object' && v !== null) return;
    // 字段存在但为 null（例如没有迟单时的平均延期）→ 保留为 null，界面显示“不适用”
    out.push({ key: d[0], label: d[1], value: v, unit: d[2], fmt: d[3] });
  });
  METRIC_DICT_DEFS.forEach(function (d) {
    const v = m[d[0]];
    seen[d[0]] = 1;
    if (!v || typeof v !== 'object') return;
    Object.keys(v).forEach(function (stage) {
      const sv = v[stage];
      if (sv === undefined || sv === null || typeof sv === 'object') return;
      out.push({
        key: d[0] + '.' + stage,
        label: d[1] + ' · ' + (STAGE_FALLBACK[stage] || stage),
        value: sv, unit: d[2], fmt: d[3]
      });
    });
  });
  // 引擎新增的标量指标同样展示，避免界面偷偷丢字段
  Object.keys(m).forEach(function (k) {
    if (seen[k]) return;
    const v = m[k];
    if (v === null || typeof v === 'object') return;
    out.push({ key: k, label: k, value: v, unit: '', fmt: typeof v === 'number' ? 'num' : '' });
  });
  return out;
}

function normGanttFromPlan(p, meta) {
  const events = Array.isArray(p.events) ? p.events : [];
  if (!events.length) return null;

  /* 事件为 B(开始含准备)/S(开始加工)/C(完成) 节点标记，无独立 day/process_seconds 字段；
     以 B→C 窗口归属日桶（按服务端日历 day_length_min 换算），时长取 C−B 分钟差。 */
  const dayLen = Number(meta && meta.dayLengthMin) || 0;
  const labels = (meta && meta.stageLabels) || STAGE_FALLBACK;
  const begin = {}, comp = {};
  const stageKeys = [];
  events.forEach(function (e) {
    if (!e || !e.stage || !e.car_id) return;
    if (stageKeys.indexOf(e.stage) < 0) stageKeys.push(e.stage);
    const k = e.stage + '|' + e.car_id;
    if (e.node === 'B' && begin[k] === undefined) begin[k] = Number(e.timestamp_min) || 0;
    if (e.node === 'C') comp[k] = Number(e.timestamp_min) || 0;
  });
  if (!stageKeys.length || !Object.keys(comp).length) return null;

  const agg = {};
  const daySet = [];
  Object.keys(comp).forEach(function (k) {
    const stage = k.split('|')[0];
    const b = begin[k] !== undefined ? begin[k] : comp[k];
    const c = comp[k];
    const day = dayLen > 0 ? Math.floor(b / dayLen) : 0;
    if (daySet.indexOf(day) < 0) daySet.push(day);
    const a = agg[stage + '|' + day] || (agg[stage + '|' + day] = { count: 0, minutes: 0 });
    a.count += 1;
    a.minutes += Math.max(0, c - b);
  });
  if (!Object.keys(agg).length) return null;
  daySet.sort(function (x, y) { return x - y; });

  const resources = stageKeys.map(function (s) { return { key: s, label: labels[s] || s, sub: '' }; });
  const buckets = daySet.map(function (d) { return { key: String(d), label: 'D' + d }; });

  const out = [];
  resources.forEach(function (r) {
    buckets.forEach(function (b) {
      const a = agg[r.key + '|' + b.key];
      if (!a) return;
      out.push({
        resourceKey: r.key,
        bucketKey: b.key,
        label: a.count + ' 台',
        sublabel: Math.round(a.minutes) + ' 分',
        tone: ''
      });
    });
  });
  if (!out.length) return null;
  return { resources: resources, buckets: buckets, events: out };
}

function normChecks(v) {
  if (!v || typeof v !== 'object') return [];
  const checks = Array.isArray(v.checks) ? v.checks : [];
  return checks.map(function (c, i) {
    if (!c || typeof c !== 'object') return null;
    return {
      item: String(c.rule || ('校验项 ' + (i + 1))),
      level: c.passed ? 'pass' : 'fail',
      detail: String(c.detail || '')
    };
  }).filter(Boolean);
}

function normUnscheduledList(list) {
  if (!Array.isArray(list)) return [];
  return list.map(function (u, i) {
    if (!u || typeof u !== 'object') return null;
    const scopeBits = [];
    if (u.model) scopeBits.push('车型 ' + u.model);
    if (u.due_day !== undefined && u.due_day !== null) scopeBits.push('需求日 D' + u.due_day);
    if (u.release_day !== undefined && u.release_day !== null) scopeBits.push('投产日 D' + u.release_day);
    return {
      order: String(u.car_id || u.order_id || ('未排项 ' + (i + 1))),
      reason: REASON_LABELS[u.reason] || String(u.reason || '未说明'),
      detail: String(u.detail || ''),
      scope: scopeBits.join(' · ')
    };
  }).filter(Boolean);
}

function normTiers(cmp) {
  if (!cmp || typeof cmp !== 'object') return [];
  const b = cmp.baseline && cmp.baseline.tiers;
  const o = cmp.optimized && cmp.optimized.tiers;
  if (!b || !o) return [];
  const delta = cmp.delta || {};
  const improved = Array.isArray(cmp.improved_tiers) ? cmp.improved_tiers : [];
  const names = (Array.isArray(cmp.objective_tiers) && cmp.objective_tiers.length)
    ? cmp.objective_tiers
    : Object.keys(TIER_LABELS);
  return names.map(function (t) {
    if (b[t] === undefined && o[t] === undefined) return null;
    const bv = b[t], ov = o[t];
    let d = delta[t];
    if (d === undefined && typeof bv === 'number' && typeof ov === 'number') d = ov - bv;
    return {
      tier: t,
      label: TIER_LABELS[t] || t,
      baseline: bv,
      optimized: ov,
      delta: (d === undefined ? null : d),
      improved: improved.indexOf(t) >= 0
    };
  }).filter(Boolean);
}

function normEngineMeta(raw) {
  const m = raw.meta || {};
  const r = raw.runtime || {};
  const rs = m.rule_scope || {};
  return {
    engine: m.engine || '',
    engineVersion: m.engine_version || '',
    inputContractVersion: m.input_contract_version || '',
    algorithm: m.algorithm || '',
    seed: m.seed,
    timeLimit: m.time_limit_seconds,
    dayLengthMin: Number(raw.calendar_day_length_min) || 0,
    stageLabels: m.stage_labels || null,
    hardRules: Array.isArray(m.hard_rules) ? m.hard_rules.map(String) : [],
    implemented: Array.isArray(rs.implemented) ? rs.implemented.map(String) : [],
    notImplemented: Array.isArray(rs.not_implemented) ? rs.not_implemented.map(String) : [],
    assumptions: Array.isArray(m.assumptions) ? m.assumptions.map(String) : [],
    caveats: Array.isArray(m.caveats) ? m.caveats.map(String) : [],
    warnings: Array.isArray(m.warnings) ? m.warnings.map(String) : [],
    runtimeStatus: r.status || '',
    runtimeSeconds: r.seconds,
    candidatesEvaluated: r.candidates_evaluated,
    candidateBudget: r.candidate_budget,
    timedOut: !!r.timed_out,
    submittable: !!raw.submittable,
    submitBlockers: Array.isArray(raw.submit_blockers) ? raw.submit_blockers.map(String) : [],
    engineError: raw.engine_error || null
  };
}

/** 由服务端真实字段拼出的确定性说明；不含任何推断性结论。 */
function normExplanationRaw(raw) {
  const cmp = raw.comparison || {};
  const r = raw.runtime || {};
  const opt = raw.optimized || {};
  const parts = [];

  if (opt.candidate_name) {
    parts.push('优化方案取自候选「' + opt.candidate_name + '」' +
      (opt.strategy ? '（' + strategyText(opt.strategy) + '）' : '') + '。');
  }
  if (cmp.optimized_not_worse_than_baseline !== undefined) {
    parts.push('在声明的词典序目标上' + (cmp.optimized_not_worse_than_baseline ? '不劣于' : '劣于') + '基线。');
  }
  if (Array.isArray(cmp.improved_tiers) && cmp.improved_tiers.length) {
    parts.push('改善项：' + cmp.improved_tiers.map(function (t) { return TIER_LABELS[t] || t; }).join('、') + '。');
  }
  if (r.status) {
    parts.push('运行状态 ' + r.status +
      (r.seconds !== undefined ? '，耗时 ' + r.seconds + ' 秒' : '') +
      '；共评估 ' + (r.candidates_evaluated !== undefined ? r.candidates_evaluated : '—') + ' 个候选。');
  }
  if (r.timed_out) parts.push('已达到求解时限，候选集未搜索完全；未找到解不代表问题无解。');
  const blockers = Array.isArray(raw.submit_blockers) ? raw.submit_blockers : [];
  if (blockers.length) parts.push('不可提交原因：' + blockers.join('；'));

  const basis = [];
  const mm = raw.meta || {};
  if (mm.engine_version) basis.push('引擎版本 ' + mm.engine_version);
  if (mm.input_contract_version) basis.push('输入契约 ' + mm.input_contract_version);
  if (mm.seed !== undefined) basis.push('随机种子 ' + mm.seed);
  if (mm.time_limit_seconds !== undefined) basis.push('求解时限 ' + mm.time_limit_seconds + ' 秒');
  if (mm.objective && Array.isArray(mm.objective.tiers)) basis.push('目标层级 ' + mm.objective.tiers.join(' > '));

  if (!parts.length && !basis.length) return null;
  return { summary: parts.join(''), basis: basis };
}

/* ============================ 10. 视图渲染 ============================ */

function render() {
  renderShell();
  const view = document.getElementById('view');
  if (!view) return;
  let html = '';
  switch (state.route.view) {
    case 'workspace': html = viewWorkspace(); break;
    case 'inbox': html = viewInbox(); break;
    case 'rules': html = viewRules(); break;
    case 'system': html = viewSystem(); break;
    case 'plans':
    default: html = viewPlans(); break;
  }
  view.innerHTML = html;
  renderBanner();
  renderConfirm();
}

/* ---- 页内确认弹层：替代原生 window.confirm ---- */

function renderConfirm() {
  const host = document.getElementById('modal');
  if (!host) return;
  const c = state.confirm;
  if (!c) { host.hidden = true; host.innerHTML = ''; return; }

  host.hidden = false;
  host.innerHTML =
    '<div class="modal" role="dialog" aria-modal="true" aria-label="' + escAttr(c.title) + '">' +
      '<div class="modal-head"><h2 class="modal-title">' + esc(c.title) + '</h2></div>' +
      '<div class="modal-body">' +
        (c.rows && c.rows.length
          ? '<div class="kv" style="margin-bottom:10px">' + c.rows.map(function (r) {
              return '<div><span class="k">' + esc(r[0]) + '</span><span class="v">' + esc(r[1]) + '</span></div>';
            }).join('') + '</div>'
          : '') +
        (c.notes || []).map(function (n) {
          return '<div class="note ' + escAttr(n.tone || 'plain') + '">' + n.html + '</div>';
        }).join('') +
      '</div>' +
      '<div class="modal-foot">' +
        '<button type="button" class="btn" data-act="confirmCancel">取消</button>' +
        '<button type="button" class="btn primary" data-act="confirmOk">' + esc(c.okLabel || '确认') + '</button>' +
      '</div>' +
    '</div>';
}

function requestSubmitConfirm(id) {
  const cand = (state.result && state.result.candidates || []).filter(function (c) { return c.key === state.selectedCandidate; })[0] || null;
  if (!state.selectedCandidate || !state.rawResult) { toast('需要先有计算结果并选定方案', 'bad'); return; }
  state.confirm = {
    kind: 'submit',
    id: String(id),
    title: '确认提交方案',
    okLabel: '确认提交',
    rows: [
      ['计划', (state.plan && (state.plan.name || state.plan.id)) || id],
      ['计划类型', typeLabel(state.plan && state.plan.type)],
      ['工厂', (state.plan && state.plan.factory) || '—'],
      ['提交方案', state.selectedCandidate],
      ['引擎判定', cand ? (cand.submittable ? '可提交' : '不可提交：' + (cand.submitBlockers || []).join('；')) : '不可用']
    ],
    notes: [
      { tone: 'bad', html: '<b>演示环境，未接入生产。</b>提交只写入本机后端状态，不会向 ERP / MES 下发，也不产生生产指令。' },
      { tone: 'bad', html: '<b>研究模型警示：</b>内核未实现人工冻结、冻结区与已投产保护，也未实现完整原文工艺规则，不能用于实际生产决策。' },
      { tone: 'warn', html: '请确认输入中<b>不含真实已投产或冻结订单</b>；提交后计划进入待审批，内容会被冻结。' }
    ]
  };
  renderConfirm();
}

function requestApproveConfirm(id) {
  const plan = (state.plan && String(state.plan.id) === String(id) ? state.plan : null) || state.plans.find(function (p) { return String(p.id) === String(id); });
  if (!plan) { toast('请先打开计划详情确认内容'); return; }
  state.confirm = {
    kind: 'approve',
    id: String(id),
    title: '确认批准该计划',
    okLabel: '确认批准',
    rows: [
      ['计划', plan.name || plan.id],
      ['状态', statusLabel(plan.status)],
      ['选定方案', plan.selected_candidate || '未选择']
    ],
    notes: [
      { tone: 'bad', html: '<b>演示环境：</b>批准只更新本机后端状态，不向 ERP / MES 下发，也不代表任何真实生效范围。' }
    ]
  };
  renderConfirm();
}

function renderShell() {
  const nav = document.getElementById('nav');
  if (nav) {
    const menus = resolveMenus();
    const active = state.route.view === 'workspace' ? 'plans' : state.route.view;
    nav.innerHTML = menus.map(function (m) {
      const on = m.key === active;
      return '<a href="#/' + escAttr(m.key) + '"' + (on ? ' class="active" aria-current="page"' : '') + '>' + esc(m.label) + '</a>';
    }).join('');
  }

  const sel = document.getElementById('userSelect');
  if (sel && !sel.dataset.ready) {
    sel.innerHTML = USERS.map(function (u) {
      return '<option value="' + escAttr(u.user) + '">' + esc(u.label) + '</option>';
    }).join('');
    sel.value = currentUser();
    sel.dataset.ready = '1';
  }

  const crumb = document.getElementById('crumb');
  const crumbSub = document.getElementById('crumbSub');
  const titles = { plans: '计划管理', inbox: '待办审批', rules: '规则资源', system: '系统' };
  if (crumb) crumb.textContent = state.route.view === 'workspace' ? '计划工作空间' : (titles[state.route.view] || '计划管理');
  if (crumbSub) {
    crumbSub.textContent = state.route.view === 'workspace'
      ? (state.plan ? (state.plan.name || state.plan.id) : (state.route.id === 'new' ? '新建计划' : ''))
      : '';
  }

  const scope = document.getElementById('scopeInfo');
  if (scope) {
    if (state.meError) scope.textContent = '身份未获取';
    else if (!state.me) scope.textContent = '加载中…';
    else {
      const fs = Array.isArray(state.me.factories) && state.me.factories.length ? state.me.factories.join(' + ') : '未授权工厂';
      scope.textContent = (state.me.user || currentUser()) + ' · ' + (state.me.role || '未知角色') + ' · ' + fs;
    }
  }

  const dot = document.getElementById('envDot');
  const envText = document.getElementById('envText');
  if (dot && envText) {
    if (state.backendOk === false) { dot.className = 'dot offline'; envText.textContent = '后端不可达'; }
    else if (state.backendOk === true) { dot.className = 'dot online'; envText.textContent = '后端已连接'; }
    else { dot.className = 'dot'; envText.textContent = '连接检测中'; }
  }
}

function renderBanner() {
  const host = document.getElementById('banner');
  if (!host) return;
  const parts = [];
  if (state.meError) {
    parts.push(bannerHtml('bad', '当前身份获取失败：' + state.meError + '（后端未启动或接口路径不一致时会这样显示）', 'me'));
  }
  if (state.plansError && state.route.view !== 'workspace') {
    parts.push(bannerHtml('bad', '计划列表加载失败：' + state.plansError, 'plans'));
  }
  if (state.planError && state.route.view === 'workspace') {
    parts.push(bannerHtml('bad', '计划加载失败：' + state.planError, 'plan'));
  }
  host.innerHTML = parts.join('');
}

function bannerHtml(tone, text, retry) {
  return '<div class="banner ' + escAttr(tone) + '">' +
    '<span class="banner-text">' + esc(text) + '</span>' +
    '<button type="button" class="btn small" data-act="bannerRetry" data-retry="' + escAttr(retry) + '">重试</button>' +
    '</div>';
}

/* ---------------- 10.1 计划管理（一级：列表 / 搜索 / 展开历史） ---------------- */

function viewPlans() {
  const kind = roleKind();
  const canCreate = kind === 'planner';
  const filtered = filteredPlans();

  const head =
    '<div class="page-head">' +
      '<div>' +
        '<h1 class="page-title">计划管理</h1>' +
        '<p class="page-desc">一级入口：计划列表、历史计算任务与结果。新建计划或打开计划进入二级工作空间（6 个步骤）。</p>' +
      '</div>' +
      '<div class="page-actions">' +
        (canCreate
          ? '<button type="button" class="btn primary" data-act="newPlan">新建计划</button>'
          : '<button type="button" class="btn" disabled title="当前角色不显示新建入口（服务端仍会校验权限）">新建计划</button>') +
        '<button type="button" class="btn" data-act="reloadPlans">刷新列表</button>' +
      '</div>' +
    '</div>';

  const stats =
    '<div class="statline">' +
      statItem('本角色可见计划', state.plans.length) +
      statItem('当前筛选结果', filtered.length) +
      statItem('数据范围', (state.me && Array.isArray(state.me.factories) && state.me.factories.length) ? state.me.factories.join(' + ') : '—', true) +
      statItem('后端状态', state.backendOk === false ? '不可达' : state.backendOk === true ? '已连接' : '检测中', true) +
    '</div>';

  const filters =
    '<div class="card"><div class="card-body">' +
      '<div class="row">' +
        '<div class="field inline"><label for="planSearch">搜索</label>' +
          '<input type="search" id="planSearch" data-bind="search" placeholder="计划名称 / 编号 / 类型 / 工厂 / 状态" value="' + escAttr(state.search) + '" style="min-width:280px"></div>' +
        '<div class="field inline"><label for="planType">类型</label>' +
          '<select id="planType" data-bind="filterType">' +
            '<option value="all"' + (state.filterType === 'all' ? ' selected' : '') + '>全部类型</option>' +
            PLAN_TYPES.map(function (t) {
              return '<option value="' + escAttr(t.key) + '"' + (state.filterType === t.key ? ' selected' : '') + '>' + esc(t.label) + '</option>';
            }).join('') +
          '</select></div>' +
        '<span class="hint">搜索与筛选只影响展示；数据范围由服务端按当前身份裁剪。</span>' +
      '</div>' +
    '</div></div>';

  const body = state.plansLoading
    ? '<div class="card"><div class="empty">正在加载计划列表…</div></div>'
    : (state.plansError ? '' : planTable(filtered));

  return head + stats + filters + body;
}

function statItem(k, v, small) {
  return '<div class="item"><span class="k">' + esc(k) + '</span><span class="v' + (small ? ' small' : '') + '">' + esc(v) + '</span></div>';
}

function filteredPlans() {
  const q = String(state.search || '').trim().toLowerCase();
  return state.plans.filter(function (p) {
    if (state.filterType !== 'all' && String(p.type) !== state.filterType) return false;
    if (!q) return true;
    const hay = [p.id, p.name, p.type, typeLabel(p.type), p.factory, p.status, statusLabel(p.status), p.created_at, p.updated_at]
      .join(' ').toLowerCase();
    return hay.indexOf(q) >= 0;
  });
}

function planTable(list) {
  if (!state.plans.length) {
    return '<div class="card"><div class="empty"><b>当前身份下没有可见计划</b>' +
      '可能原因：服务端按角色与工厂裁剪了数据，或尚未创建计划。' +
      (roleKind() === 'planner' ? '可点击右上角“新建计划”创建。' : '当前角色不显示新建入口。') +
      '</div></div>';
  }
  if (!list.length) {
    return '<div class="card"><div class="empty"><b>没有匹配的计划</b>请调整搜索关键词或类型筛选。</div></div>';
  }

  return '<div class="card"><div class="card-head">' +
      '<h2 class="card-title">计划列表</h2>' +
      '<span class="card-sub">共 ' + state.plans.length + ' 条 · 表格为紧凑视图，点击“展开”查看历史任务与结果</span>' +
    '</div><div class="card-body tight"><div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr>' +
        '<th style="width:64px">详情</th><th>计划名称</th><th>类型</th><th>工厂</th><th>状态</th>' +
        '<th>输入</th><th>任务</th><th>结果</th><th>更新时间</th>' +
      '</tr></thead><tbody id="planTableBody">' + planRowsHtml(list) + '</tbody></table></div></div></div>';
}

function planRowsHtml(list) {
  if (!list.length) {
    return '<tr><td colspan="9" class="tbl-empty">没有匹配的计划，请调整搜索关键词或类型筛选。</td></tr>';
  }
  return list.map(function (p) {
    const id = String(p.id);
    const open = state.expanded === id;
    const detail = state.details[id];
    const tasks = Array.isArray(p.tasks) ? p.tasks : [];
    const running = tasks.filter(function (t) { return taskIsRunning(t); }).length;

    let html =
      '<tr>' +
        '<td class="row-actions"><button type="button" class="btn small" data-act="toggleDetail" data-id="' + escAttr(id) + '" aria-expanded="' + (open ? 'true' : 'false') + '">' + (open ? '收起' : '展开') + '</button></td>' +
        '<td><a href="#/plan/' + escAttr(id) + '/1">' + esc(p.name || ('计划 ' + id)) + '</a></td>' +
        '<td>' + esc(typeLabel(p.type)) + (isComputable(p.type) ? '' : ' ' + tagHtml('计算未实现', 'warn')) + '</td>' +
        '<td>' + esc(p.factory || '—') + '</td>' +
        '<td>' + tagHtml(statusLabel(p.status), statusTone(p.status)) + '</td>' +
        '<td>' + (p.input ? tagHtml('已保存', 'cyan') : tagHtml('未保存', 'plain')) + '</td>' +
        '<td>' + tasks.length + (running ? ' ' + tagHtml(running + ' 运行中', 'warn') : '') + '</td>' +
        '<td>' + resultSummary(p) + '</td>' +
        '<td class="mono">' + esc(fmtTime(p.updated_at || p.created_at)) + '</td>' +
      '</tr>';

    if (open) {
      html += '<tr><td colspan="9" style="background:#fbfdff">' + detailPanel(id, p, detail) + '</td></tr>';
    }
    return html;
  }).join('');
}

function resultSummary(p) {
  if (!p.result) return '<span class="muted">无结果</span>';
  const n = normalizeResult(p.result, p);
  if (!n) return tagHtml('有原始结果 · 结构待接入', 'warn');
  return tagHtml(n.candidates.length + ' 个候选', 'ok');
}

function detailPanel(id, plan, detail) {
  if (!detail || detail.loading) return '<div class="hint" style="padding:8px">正在加载计划详情…</div>';
  if (detail.error) {
    return '<div class="note bad" style="margin:8px">详情加载失败：' + esc(detail.error) +
      ' <button type="button" class="btn small" data-act="reloadDetail" data-id="' + escAttr(id) + '">重试</button></div>';
  }
  const d = detail.plan || {};
  const tasks = Array.isArray(d.tasks) ? d.tasks : [];

  const taskRows = tasks.length
    ? tasks.map(function (t) {
        const running = taskIsRunning(t);
        return '<tr>' +
          '<td class="mono">' + esc(t.id) + '</td>' +
          '<td>' + tagHtml(statusLabel(t.status), running ? 'warn' : statusTone(t.status)) + '</td>' +
          '<td class="mono">' + esc(fmtTime(t.created_at)) + '</td>' +
          '<td class="row-actions"><button type="button" class="btn small" data-act="openTask" data-id="' + escAttr(id) + '" data-task="' + escAttr(t.id) + '">' +
            (running ? '查看进度' : '查看结果') + '</button></td>' +
        '</tr>';
      }).join('')
    : '<tr><td colspan="4" class="tbl-empty">尚无计算任务记录</td></tr>';

  const rn = d.result ? normalizeResult(d.result, d) : null;

  return '<div style="padding:10px 4px">' +
    '<div class="kv" style="margin-bottom:10px">' +
      '<div><span class="k">计划编号</span><span class="v mono">' + esc(d.id) + '</span></div>' +
      '<div><span class="k">计划类型</span><span class="v">' + esc(typeLabel(d.type)) + '</span></div>' +
      '<div><span class="k">工厂</span><span class="v">' + esc(d.factory || '—') + '</span></div>' +
      '<div><span class="k">状态</span><span class="v">' + tagHtml(statusLabel(d.status), statusTone(d.status)) + '</span></div>' +
      '<div><span class="k">创建时间</span><span class="v">' + esc(fmtTime(d.created_at)) + '</span></div>' +
      '<div><span class="k">选定方案</span><span class="v">' + esc(d.selected_candidate || '未选择') + '</span></div>' +
    '</div>' +
    '<div class="card" style="margin-bottom:10px"><div class="card-head"><h3 class="card-title">历史计算任务</h3>' +
      '<span class="card-sub">任务由服务端保存，离开页面不会中断</span></div>' +
      '<div class="card-body tight"><div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr><th>任务 ID</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>' +
      '<tbody>' + taskRows + '</tbody></table></div></div></div>' +
    '<div class="row">' +
      '<button type="button" class="btn primary" data-act="openPlan" data-id="' + escAttr(id) + '" data-step="4">打开工作空间</button>' +
      '<span class="hint">' + (rn
        ? '结果已归一化：' + esc(rn.candidates.map(function (c) { return c.label; }).join('、'))
        : (d.result ? '存在原始结果但结构尚未接入 normalizeResult，请在“方案分析”查看原始 JSON。' : '尚无计算结果。')) + '</span>' +
    '</div>' +
  '</div>';
}

/* ---------------- 10.2 待办（主管） ---------------- */

function viewInbox() {
  if (roleKind() !== 'manager') {
    return '<div class="page-head"><div><h1 class="page-title">待办审批</h1>' +
      '<p class="page-desc">该菜单仅对生产主管开放；当前角色不显示审批入口。</p></div></div>' +
      '<div class="card"><div class="empty"><b>当前角色无审批权</b>审批由主管执行，且由服务端校验身份，前端隐藏不等于授权。</div></div>';
  }

  const pending = state.plans.filter(function (p) { return isPendingApproval(p.status); });
  const others = state.plans.filter(function (p) { return !isPendingApproval(p.status); });

  const pendingTable = pending.length
    ? '<div class="card"><div class="card-body tight"><div class="tbl-wrap"><table class="tbl">' +
        '<thead><tr><th>计划名称</th><th>类型</th><th>工厂</th><th>状态</th><th>选定方案</th><th>提交时间</th><th>操作</th></tr></thead><tbody>' +
        pending.map(function (p) {
          const id = String(p.id);
          return '<tr>' +
            '<td><a href="#/plan/' + escAttr(id) + '/6">' + esc(p.name || ('计划 ' + id)) + '</a></td>' +
            '<td>' + esc(typeLabel(p.type)) + '</td>' +
            '<td>' + esc(p.factory || '—') + '</td>' +
            '<td>' + tagHtml(statusLabel(p.status), 'warn') + '</td>' +
            '<td>' + esc(p.selected_candidate || '未选择') + '</td>' +
            '<td class="mono">' + esc(fmtTime(p.updated_at || p.created_at)) + '</td>' +
            '<td class="row-actions">' +
              '<button type="button" class="btn primary small" data-act="approve" data-id="' + escAttr(id) + '">批准</button> ' +
              '<button type="button" class="btn small" data-act="openPlan" data-id="' + escAttr(id) + '" data-step="6">查看内容</button>' +
            '</td>' +
          '</tr>';
        }).join('') +
        '</tbody></table></div></div></div>'
    : '<div class="card"><div class="empty"><b>当前没有待审批计划</b>计划员提交后会出现在这里。审批仅改变计划状态，不向 ERP / MES 下发任何指令。</div></div>';

  return '<div class="page-head"><div>' +
      '<h1 class="page-title">待办审批</h1>' +
      '<p class="page-desc">主管职责：审批计划员提交的内容版本。编制与批准职责分离，计划员不能自行批准。</p>' +
    '</div><div class="page-actions"><button type="button" class="btn" data-act="reloadPlans">刷新</button></div></div>' +
    '<div class="note warn">演示环境：批准操作仅写入本机后端状态，不会发布到生产系统。</div>' +
    pendingTable +
    '<div class="card"><div class="card-head"><h2 class="card-title">其他计划</h2><span class="card-sub">供主管了解进度，不改变其状态</span></div>' +
      '<div class="card-body tight">' + (others.length ? smallPlanTable(others) : '<div class="empty">没有其他计划</div>') + '</div></div>';
}

function isPendingApproval(s) {
  const k = String(s || '').toLowerCase();
  return /submitted|pending|approv|待审批/.test(k) && !/approved/.test(k);
}

function smallPlanTable(list) {
  return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>计划名称</th><th>类型</th><th>工厂</th><th>状态</th><th>更新时间</th></tr></thead><tbody>' +
    list.map(function (p) {
      return '<tr><td><a href="#/plan/' + escAttr(String(p.id)) + '/1">' + esc(p.name || p.id) + '</a></td>' +
        '<td>' + esc(typeLabel(p.type)) + '</td><td>' + esc(p.factory || '—') + '</td>' +
        '<td>' + tagHtml(statusLabel(p.status), statusTone(p.status)) + '</td>' +
        '<td class="mono">' + esc(fmtTime(p.updated_at || p.created_at)) + '</td></tr>';
    }).join('') + '</tbody></table></div>';
}

/* ---------------- 10.3 规则资源（工艺，只读） ---------------- */

function viewRules() {
  const kind = roleKind();
  if (kind !== 'engineer' && kind !== 'admin') {
    return '<div class="page-head"><div><h1 class="page-title">规则资源</h1>' +
      '<p class="page-desc">该菜单面向工艺/产能负责人；当前角色不显示此菜单。</p></div></div>' +
      '<div class="card"><div class="empty"><b>当前角色无规则资源视图</b>菜单由服务端 /api/me 的 menus 决定。</div></div>';
  }

  const sample = state.sample;
  const sections = extractRuleSections(sample);

  let body;
  if (state.sampleError) {
    body = '<div class="card"><div class="empty"><b>规则资源数据接口未接入</b>' +
      '约定接口中没有规则/资源列表接口，当前页面尝试读取 /api/sample 也不可用：' + esc(state.sampleError) + '</div></div>';
  } else if (!sections.length) {
    body = '<div class="card"><div class="empty"><b>暂无可展示的规则或资源数据</b>' +
      '约定 API 中尚无规则资源端点（GET /api/sample 未包含 rules / resources / constraints 字段）。' +
      '本页不伪造规则内容，待主会话补充接口后在此展示。</div></div>';
  } else {
    body = sections.map(function (s) {
      return '<div class="card"><div class="card-head"><h2 class="card-title">' + esc(s.label) + '</h2>' +
        '<span class="card-sub">来源：/api/sample · 只读</span></div>' +
        '<div class="card-body tight">' + renderLooseTable(s.value) + '</div></div>';
    }).join('');
  }

  return '<div class="page-head"><div>' +
      '<h1 class="page-title">规则资源</h1>' +
      '<p class="page-desc">工艺/产能负责人视图：规则与资源版本为只读展示，规则维护与批准由服务端负责。</p>' +
    '</div><div class="page-actions"><button type="button" class="btn" data-act="reloadSample">重新载入</button></div></div>' +
    '<div class="note info">本页不提供编辑与提交入口；规则变更必须走服务端版本化流程，历史计划仍引用旧版规则。</div>' +
    body;
}

function extractRuleSections(obj) {
  if (!obj || typeof obj !== 'object') return [];
  const wanted = ['rules', 'resources', 'constraints', 'constraint', 'calendars', 'shifts', 'objectives', 'goals', 'capacity'];
  const out = [];
  wanted.forEach(function (k) {
    Object.keys(obj).forEach(function (key) {
      if (key.toLowerCase() === k) out.push({ label: key, value: obj[key] });
    });
  });
  if (!out.length && Array.isArray(obj)) out.push({ label: '数据项', value: obj });
  return out;
}

function renderLooseTable(value) {
  if (Array.isArray(value) && value.length) {
    const isObj = value.some(function (v) { return v && typeof v === 'object'; });
    if (!isObj) {
      return '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>值</th></tr></thead><tbody>' +
        value.map(function (v) { return '<tr><td>' + esc(v) + '</td></tr>'; }).join('') + '</tbody></table></div>';
    }
    const cols = [];
    value.forEach(function (r) {
      if (r && typeof r === 'object') Object.keys(r).forEach(function (k) { if (cols.indexOf(k) < 0) cols.push(k); });
    });
    return '<div class="tbl-wrap"><table class="tbl"><thead><tr>' + cols.map(function (c) { return '<th>' + esc(c) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + value.map(function (r) {
        return '<tr>' + cols.map(function (c) {
          const v = r ? r[c] : '';
          return '<td>' + esc(v && typeof v === 'object' ? safeJson(v) : v) + '</td>';
        }).join('') + '</tr>';
      }).join('') + '</tbody></table></div>';
  }
  if (value && typeof value === 'object') {
    return '<div class="kv" style="padding:10px 12px">' + Object.keys(value).map(function (k) {
      const v = value[k];
      return '<div><span class="k">' + esc(k) + '</span><span class="v">' + esc(v && typeof v === 'object' ? safeJson(v) : v) + '</span></div>';
    }).join('') + '</div>';
  }
  return '<div class="empty">' + esc(String(value)) + '</div>';
}

/* ---------------- 10.4 系统（管理员，无审批） ---------------- */

function viewSystem() {
  if (roleKind() !== 'admin') {
    return '<div class="page-head"><div><h1 class="page-title">系统</h1>' +
      '<p class="page-desc">该菜单面向系统管理员；当前角色不显示。</p></div></div>' +
      '<div class="card"><div class="empty"><b>当前角色无系统视图</b>系统菜单不包含业务审批权限，管理员默认不具备业务批准权。</div></div>';
  }

  const endpointRows = [
    ['GET', '/api/me', '当前身份、角色、工厂与菜单（plans/inbox/rules/system）'],
    ['GET', '/api/plans', '计划列表（服务端按身份裁剪）'],
    ['POST', '/api/plans', '新建计划 {name,type,factory}'],
    ['GET', '/api/plans/{id}', '计划详情'],
    ['PATCH', '/api/plans/{id}', '更新 {input} 或 {name}；保存输入会清除旧结果，任务历史保留'],
    ['GET', '/api/sample', '原始 Mock 输入 JSON'],
    ['POST', '/api/plans/{id}/run', '发起计算，返回 {task_id}；非 W+3 返回 422'],
    ['GET', '/api/tasks/{id}', '任务状态、阶段、进度日志与结果'],
    ['POST', '/api/plans/{id}/submit', '提交方案 {candidate}'],
    ['POST', '/api/plans/{id}/approve', '主管批准'],
    ['GET', '/api/system', '仅管理员：运行数与审计记录']
  ];

  const sys = state.system;
  const sysCard = state.systemError
    ? '<div class="card"><div class="card-head"><h2 class="card-title">运行数与审计</h2><span class="card-sub">GET /api/system</span></div>' +
      '<div class="card-body"><div class="note bad">加载失败：' + esc(state.systemError) + '</div>' +
      '<div class="hint" style="margin-top:6px">该接口仅对管理员开放，其他身份会返回 403。</div></div></div>'
    : '<div class="card"><div class="card-head"><h2 class="card-title">运行数与审计</h2><span class="card-sub">GET /api/system</span></div>' +
      '<div class="card-body">' +
        '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>统计项</th><th class="num">数值</th></tr></thead><tbody>' +
          (sys ? Object.keys(sys).filter(function (k) { return typeof sys[k] !== 'object' && sys[k] !== null; })
            .map(function (k) { return '<tr><td>' + esc(k) + '</td><td class="num">' + esc(sys[k]) + '</td></tr>'; }).join('') : '') +
          (sys && Object.keys(sys).filter(function (k) { return typeof sys[k] !== 'object' && sys[k] !== null; }).length ? '' :
            '<tr><td colspan="2" class="tbl-empty">暂无标量统计字段</td></tr>') +
        '</tbody></table></div>' +
        '<details class="raw" style="margin-top:10px"><summary>查看原始 JSON</summary><pre>' +
          esc(sys ? safeJson(sys) : '（加载中）') + '</pre></details>' +
      '</div></div>';

  return '<div class="page-head"><div>' +
      '<h1 class="page-title">系统</h1>' +
      '<p class="page-desc">运行环境与接口状态。管理员菜单不包含审批功能，审批仅由主管在待办中执行。</p>' +
    '</div><div class="page-actions">' +
      '<button type="button" class="btn" data-act="reloadMe">重测连接</button>' +
      '<button type="button" class="btn" data-act="reloadSystem">刷新运行数</button></div></div>' +

    '<div class="note plain">演示环境：后端默认监听 127.0.0.1:18744，与本页面同源时无需额外配置。' +
      '管理员菜单不含审批入口；审批只由生产主管在待办中执行。</div>' +

    sysCard +

    '<div class="card"><div class="card-head"><h2 class="card-title">运行环境</h2></div><div class="card-body">' +
      '<div class="kv">' +
        '<div><span class="k">API 基地址</span><span class="v mono">' + esc(apiBase() || '同源（页面所在服务）') + '</span></div>' +
        '<div><span class="k">当前模拟身份</span><span class="v mono">' + esc(currentUser()) + '</span></div>' +
        '<div><span class="k">后端连通</span><span class="v">' + esc(state.backendOk === false ? '不可达' : state.backendOk === true ? '已连接' : '检测中') + '</span></div>' +
        '<div><span class="k">X-Demo-User</span><span class="v">' + esc(currentUser()) + '（演示用请求头，非生产鉴权）</span></div>' +
      '</div>' +
      (state.backendOk === false ? '<div class="note bad" style="margin-top:10px">后端不可达：请确认后端服务已启动，且页面与接口同源（或在下方设置 API 基地址）。</div>' : '') +
    '</div></div>' +

    '<div class="card"><div class="card-head"><h2 class="card-title">/api/me 原始返回</h2><span class="card-sub">服务端下发的菜单与数据范围以此为准</span></div>' +
      '<div class="card-body"><details class="raw" open><summary>查看 JSON</summary><pre>' +
        esc(state.me ? safeJson(state.me) : (state.meError ? '（获取失败：' + state.meError + '）' : '（加载中）')) +
      '</pre></details></div></div>' +

    '<div class="card"><div class="card-head"><h2 class="card-title">接口约定</h2><span class="card-sub">前端当前依赖的端点清单</span></div>' +
      '<div class="card-body tight"><div class="tbl-wrap"><table class="tbl"><thead><tr><th>方法</th><th>路径</th><th>用途</th></tr></thead><tbody>' +
        endpointRows.map(function (r) {
          return '<tr><td class="mono">' + esc(r[0]) + '</td><td class="mono">' + esc(r[1]) + '</td><td>' + esc(r[2]) + '</td></tr>';
        }).join('') +
      '</tbody></table></div></div></div>' +

    '<div class="card"><div class="card-head"><h2 class="card-title">计算能力</h2><span class="card-sub">未实现的类型不会伪装成已实现</span></div>' +
      '<div class="card-body tight"><div class="tbl-wrap"><table class="tbl"><thead><tr><th>计划类型</th><th>计算能力</th><th>说明</th></tr></thead><tbody>' +
        PLAN_TYPES.map(function (t) {
          return '<tr><td>' + esc(t.label) + '</td><td>' + (t.computable ? tagHtml('可运行', 'ok') : tagHtml('未实现', 'warn')) + '</td><td>' + esc(t.note) + '</td></tr>';
        }).join('') +
      '</tbody></table></div></div></div>' +

    '<div class="card"><div class="card-head"><h2 class="card-title">演示数据样例</h2><span class="card-sub">GET /api/sample</span></div>' +
      '<div class="card-body">' +
        (state.sampleError
          ? '<div class="note bad">样例加载失败：' + esc(state.sampleError) + '</div>'
          : '<details class="raw"><summary>查看原始 Mock JSON</summary><pre>' + esc(state.sample ? safeJson(state.sample) : '（加载中）') + '</pre></details>') +
        '<div class="note plain" style="margin-top:8px">样例仅用于接口结构联调，不代表任何真实业务规则或产能数据。</div>' +
      '</div></div>' +

    '<div class="card"><div class="card-head"><h2 class="card-title">API 基地址</h2></div><div class="card-body">' +
      '<div class="row"><input type="text" id="apiBaseInput" placeholder="留空表示同源；跨端口示例 http://127.0.0.1:18744" value="' + escAttr(apiBase()) + '" style="min-width:340px">' +
      '<button type="button" class="btn" data-act="saveApiBase">保存并重连</button></div>' +
      '<div class="hint" style="margin-top:6px">仅在前后端不同端口时需要设置；设置保存在浏览器本地，不写入后端。</div>' +
    '</div></div>';
}

/* ---------------- 10.5 二级工作空间（6 步） ---------------- */

function viewWorkspace() {
  const id = state.route.id;
  const isNew = !state.plan;

  if (state.planLoading) {
    return '<div class="card"><div class="empty">正在加载计划…</div></div>';
  }
  if (state.planError && !isNew) {
    return '<div class="card"><div class="empty"><b>计划加载失败</b>' + esc(state.planError) +
      '<div style="margin-top:10px"><a class="btn" href="#/plans">返回计划列表</a></div></div></div>';
  }
  if (isNew && id !== 'new') {
    return '<div class="card"><div class="empty"><b>计划不存在或不可见</b>' +
      '服务端可能按当前身份隐藏了该计划。' +
      '<div style="margin-top:10px"><a class="btn" href="#/plans">返回计划列表</a></div></div></div>';
  }

  let step = state.route.step;
  if (isNew) step = 1;

  const plan = state.plan;
  const head =
    '<div class="page-head">' +
      '<div>' +
        '<h1 class="page-title">' + (isNew ? '新建计划' : esc(plan.name || ('计划 ' + plan.id))) + '</h1>' +
        '<p class="page-desc">' +
          (isNew
            ? '填写基本信息后创建计划；创建会写入后端并可随时回到列表继续。'
            : esc(typeLabel(plan.type)) + ' · ' + esc(plan.factory || '未指定工厂') + ' · ' + statusLabel(plan.status)) +
        '</p>' +
      '</div>' +
      '<div class="page-actions">' +
        '<a class="btn" href="#/plans">返回列表</a>' +
        (isNew ? '' : '<button type="button" class="btn" data-act="reloadPlan" data-id="' + escAttr(String(plan.id)) + '">重新读取</button>') +
      '</div>' +
    '</div>';

  const meta = isNew ? '' :
    '<div class="statline" style="margin-bottom:12px">' +
      statItem('计划编号', plan.id, true) +
      statItem('类型', typeLabel(plan.type), true) +
      statItem('工厂', plan.factory || '—', true) +
      statItem('状态', statusLabel(plan.status), true) +
      statItem('选定方案', plan.selected_candidate || '未选择', true) +
      statItem('最近更新', fmtTime(plan.updated_at || plan.created_at), true) +
    '</div>';

  const steps = '<div class="steps">' + STEP_NAMES.map(function (name, i) {
    const n = i + 1;
    const cls = n === step ? 'active' : (n < step ? 'done' : '');
    const disabled = isNew && n > 1;
    return '<button type="button" class="step ' + cls + '" data-act="gotoStep" data-step="' + n + '"' +
      (disabled ? ' disabled title="请先完成基本信息"' : '') + '>' +
      '<span class="idx">' + n + '</span>' + esc(name) + '</button>';
  }).join('') + '</div>';

  let body = '';
  switch (step) {
    case 1: body = stepBasic(isNew, plan); break;
    case 2: body = stepInput(plan); break;
    case 3: body = stepConstraints(plan); break;
    case 4: body = stepCompute(plan); break;
    case 5: body = stepAnalysis(plan); break;
    case 6: body = stepSubmit(plan); break;
    default: body = stepBasic(isNew, plan);
  }

  return head + meta +
    '<div class="note bad" style="margin-bottom:12px"><b>研究模型 / 演示环境：</b>' +
      '计算结果来自研究性内核，未实现人工冻结、冻结区与已投产保护，也未实现完整原文工艺规则；' +
      '平台不向 ERP / MES 下发任何内容。<b>请勿输入真实已投产或冻结订单。</b></div>' +
    steps + body;
}

/* ---- 步骤 1：基本信息 ---- */
function stepBasic(isNew, plan) {
  const factories = (state.me && Array.isArray(state.me.factories) && state.me.factories.length) ? state.me.factories : [];
  const factoryOpts = (factories.length ? factories : ['F1', 'F2']).map(function (f) {
    return '<option value="' + escAttr(f) + '">' + esc(f) + '</option>';
  }).join('');

  if (isNew) {
    if (state.newPlan.type === '' ) state.newPlan.type = 'W+3';
    if (!state.newPlan.factory && factories.length) state.newPlan.factory = factories[0];
    const t = TYPE_MAP[state.newPlan.type];

    return '<div class="card"><div class="card-head"><h2 class="card-title">第 1 步 · 基本信息</h2>' +
        '<span class="card-sub">创建后写入后端，可离开页面稍后继续</span></div>' +
      '<div class="card-body">' +
        '<div class="form-grid">' +
          '<div class="field"><label for="npName">计划名称</label>' +
            '<input type="text" id="npName" data-bind="newPlan.name" placeholder="例如 W+3 周计划 2026-W39" value="' + escAttr(state.newPlan.name) + '"></div>' +
          '<div class="field"><label for="npType">计划类型</label>' +
            '<select id="npType" data-bind="newPlan.type">' + PLAN_TYPES.map(function (p) {
              return '<option value="' + escAttr(p.key) + '"' + (state.newPlan.type === p.key ? ' selected' : '') + '>' + esc(p.label) + '</option>';
            }).join('') + '</select></div>' +
          '<div class="field"><label for="npFactory">工厂</label>' +
            '<select id="npFactory" data-bind="newPlan.factory">' + factoryOpts + '</select></div>' +
        '</div>' +
        '<div class="note ' + (t && t.computable ? 'ok' : 'warn') + '" style="margin-top:12px">' +
          '<b>' + esc(t ? t.label : state.newPlan.type) + '</b>：' + esc(t ? t.note : '未知类型') +
        '</div>' +
        (roleKind() === 'planner'
          ? '<div class="row" style="margin-top:12px"><button type="button" class="btn primary" data-act="createPlan">创建计划并继续</button>' +
            '<span class="hint">创建后类型与工厂不可更改（当前接口未提供变更能力）。</span></div>'
          : '<div class="note warn" style="margin-top:12px">当前角色不显示新建入口；即使显示，服务端仍会校验权限。</div>') +
        '<div class="note plain" style="margin-top:10px">演示环境：创建的计划仅保存在本机后端，不会同步到 ERP / MES。</div>' +
      '</div></div>';
  }

  const nameEditable = canEditPlanInput() && !statusLocked();
  return '<div class="card"><div class="card-head"><h2 class="card-title">第 1 步 · 基本信息</h2>' +
      '<span class="card-sub">' + (nameEditable ? '名称可修改，保存后写入后端' : '当前状态或角色不可修改') + '</span></div>' +
    '<div class="card-body">' +
      '<div class="form-grid">' +
        '<div class="field"><label for="pName">计划名称</label>' +
          '<input type="text" id="pName" data-bind="planDraftName" value="' + escAttr(plan.name || '') + '"' + (nameEditable ? '' : ' disabled') + '></div>' +
        '<div class="field"><label>计划类型</label><input type="text" value="' + escAttr(typeLabel(plan.type)) + '" disabled></div>' +
        '<div class="field"><label>工厂</label><input type="text" value="' + escAttr(plan.factory || '—') + '" disabled></div>' +
      '</div>' +
      '<div class="row" style="margin-top:12px">' +
        '<button type="button" class="btn primary" data-act="saveName" data-id="' + escAttr(String(plan.id)) + '"' + (nameEditable ? '' : ' disabled') + '>保存名称</button>' +
        '<a class="btn" href="#/plan/' + escAttr(String(plan.id)) + '/2">下一步：接口输入</a>' +
      '</div>' +
      (statusLockReason() ? '<div class="note warn" style="margin-top:10px">' + esc(statusLockReason()) + '</div>' : '') +
      '<div class="note ' + (isComputable(plan.type) ? 'ok' : 'warn') + '" style="margin-top:10px">' +
        esc(typeNote(plan.type)) + '</div>' +
    '</div></div>';
}

/* ---- 步骤 2：接口输入 ---- */
function stepInput(plan) {
  const editable = canEditPlanInput() && !statusLocked();
  const locked = statusLocked();
  const hasInput = !!(plan && plan.input);
  const jsonState = jsonStateHtml(state.editor.inputText);
  const scenarios = state.presetScenarios || [];

  const scCards = scenarios.map(function (sc) {
    const isCur = hasInput && plan.input && plan.input.schema === 'aps_fullchain_v1_xml' && plan.input.scenario_id === sc.id;
    return '<div class="scenario-card" data-act="loadXmlScenario" data-id="' + escAttr(sc.id) + '" title="点击加载该接口实例">' +
      '<div class="scenario-card-head">' +
        '<span class="tag ' + escAttr(sc.tag_color || 'plain') + '">' + esc(sc.badge) + '</span>' +
        (isCur ? '<span class="tag cyan">当前已加载</span>' : '') +
      '</div>' +
      '<div class="scenario-card-title">' + esc(sc.name) + '</div>' +
      '<div class="scenario-card-desc">' + esc(sc.description) + '</div>' +
      '<div class="scenario-card-focus">核心焦点：' + esc(sc.focus) + '</div>' +
      '<div class="scenario-card-foot">推荐算法：<b>' + esc(sc.recommended_strategy.replace('Strategy_', '')) + '</b></div>' +
    '</div>';
  }).join('');

  return '<div class="card"><div class="card-head"><h2 class="card-title">第 2 步 · 接口输入</h2>' +
      '<span class="card-sub">支持标准 XML 接口实例载入，或手动编辑 Mock JSON；保存写入后端</span></div>' +
    '<div class="card-body">' +
      '<div class="preset-scenarios-host">' +
        '<div class="preset-scenarios-title">典型业务场景接口实例库 (XML报文)</div>' +
        '<div class="preset-scenarios-grid">' + scCards + '</div>' +
        '<div class="hint" style="margin-top:10px">点击上方卡片直接将企业 XML 接口实例载入本计划；载入后自动生成可读需求摘要。</div>' +
      '</div>' +

      (hasInput && plan.input.schema === 'aps_fullchain_v1_xml' ? (
        '<div class="xml-summary-box" style="margin-top:16px; padding:16px; border:1px solid #e2e8f0; border-radius:8px; background:#f8fafc">' +
          '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px">' +
            '<h3 style="margin:0; font-size:14px">接口实例解析与需求净额摘要</h3>' +
            '<button type="button" class="btn small" data-act="toggleXmlPreview">查看/隐藏 XML 报文</button>' +
          '</div>' +
          '<div class="statline">' +
            statItem('场景标识', plan.input.summary.scenario_id, true) +
            statItem('车辆订单', plan.input.summary.orders_total + ' 辆', true) +
            statItem('拔出车辆', plan.input.summary.pullout_orders + ' 辆', true) +
            statItem('外部事件', plan.input.summary.events_count + ' 项', true) +
          '</div>' +
          '<div class="statline" style="margin-top:8px">' +
            statItem('车型分布', JSON.stringify(plan.input.summary.models_breakdown), false) +
            statItem('颜色分布', JSON.stringify(plan.input.summary.colors_breakdown), false) +
          '</div>' +
          '<div id="xmlPreviewContainer" style="display:none; margin-top:16px">' +
            '<div class="hint" style="margin-bottom:6px">原始 XML 接口报文：</div>' +
            '<textarea id="rawXmlPreview" readonly rows="12" spellcheck="false" style="font-family:ui-monospace,monospace; font-size:12px">' + esc(plan.input.raw_xml) + '</textarea>' +
          '</div>' +
        '</div>'
      ) : '') +

      '<div class="divider" style="margin: 20px 0; border-top: 1px dashed #cbd5e1"></div>' +

      '<div class="row">' +
        '<button type="button" class="btn" data-act="loadSample"' + (editable ? '' : ' disabled') + '>载入服务端样例</button>' +
        '<button type="button" class="btn" data-act="reloadInput"' + (editable ? '' : ' disabled') + '>重载已保存输入</button>' +
        '<label class="btn file-label">上传 Mock JSON' +
          '<input type="file" accept=".json,application/json" data-act="uploadMock"' + (editable ? '' : ' disabled') + '></label>' +
        '<button type="button" class="btn" data-act="formatInput"' + (editable ? '' : ' disabled') + '>格式化</button>' +
        '<button type="button" class="btn cyan" data-act="saveInput" data-id="' + escAttr(String(plan.id)) + '"' + (editable ? '' : ' disabled') + '>保存输入</button>' +
        '<span id="jsonState">' + jsonState + '</span>' +
      '</div>' +
      '<div class="row" style="margin-top:8px; align-items:flex-start">' +
        '<textarea id="inputJson" data-bind="editor.inputText" spellcheck="false" rows="12"' +
          (editable ? '' : ' readonly') + ' placeholder="粘贴输入 JSON，或点击上方场景卡片载入 XML 接口实例">' + esc(state.editor.inputText) + '</textarea>' +
      '</div>' +
      '<div class="row" style="margin-top:8px">' +
        (state.editor.dirty ? '<span class="tag warn">有未保存修改</span>' : '<span class="tag plain">与服务端一致</span>') +
        '<span class="hint">当前 ' + (hasInput ? '服务端已保存输入' : '服务端尚无输入') + '；输入保存后计算才使用该快照。</span>' +
      '</div>' +
      (locked ? '<div class="note warn" style="margin-top:10px">' + esc(statusLockReason()) + '</div>' : '') +
      '<div class="note warn" style="margin-top:10px">保存输入会清除该计划已有的计算结果（需重新发起计算）；历史任务记录仍会保留。</div>' +
      '<div class="note plain" style="margin-top:8px">Mock 数据仅用于接口结构联调，不代表任何真实业务规则或产能数据；正式参数以已确认的业务规则为准。</div>' +
    '</div></div>';
}

function jsonStateHtml(text) {
  const t = String(text || '').trim();
  if (!t) return '<span class="tag plain">JSON 为空</span>';
  try { JSON.parse(t); return '<span class="tag ok">JSON 合法</span>'; }
  catch (e) { return '<span class="tag bad">JSON 错误：' + esc(String(e.message).slice(0, 80)) + '</span>'; }
}

/* ---- 步骤 3：约束目标 ---- */
/* 引擎实际解析的输入字段（engine/model.py 的 normalize()）。
   不在此列表中的字段会被引擎静默忽略，因此界面不提供该假象入口。 */
const ENGINE_INPUT_SCHEMA = {
  orders: '订单/需求明细（步骤 2 编辑原始 JSON）',
  calendar: '工作日历与每日产能',
  supply: '关键物料累计供应曲线',
  process_times: '各阶段节拍（默认值 + 按车型覆盖）',
  changeover: '切换规则（切换键 + 切换秒数）',
  transfer_seconds: '阶段间流转时间',
  seed: '随机种子（影响启发式搜索路径）',
  time_limit_seconds: '求解时限（秒，必须 > 0）',
  drain_days: '收尾天数（>= 0）'
};

const OBJECTIVE_TIERS_UI = [
  ['unscheduled_cars', '未排车辆'],
  ['late_cars', '延期车辆'],
  ['tardiness_days', '延期天数合计'],
  ['flow_span_days', '流转跨度（天）'],
  ['changeover_seconds', '切换总时长（秒）'],
  ['total_wait_seconds', '等待总时长（秒）']
];

function schemaNum(path, label, step, disabled) {
  const v = getPath(state.schema || {}, path);
  return '<div class="field"><label>' + esc(label) + '</label>' +
    '<input type="number" step="' + escAttr(step || '1') + '" data-bind="schema.' + escAttr(path) + '" data-cast="number"' +
    (disabled ? ' disabled' : '') +
    ' value="' + escAttr(v === undefined || v === null ? '' : v) + '"></div>';
}

function bindInput(path, value, disabled, width) {
  return '<input type="text" data-bind="' + escAttr(path) + '" value="' + escAttr(value === undefined || value === null ? '' : value) + '"' +
    (disabled ? ' disabled' : '') + (width ? ' style="width:' + escAttr(width) + '"' : '') + '>';
}

function bindNumber(path, value, disabled, width, step) {
  return '<input type="number" step="' + escAttr(step || '1') + '" data-bind="' + escAttr(path) + '" data-cast="number" value="' +
    escAttr(value === undefined || value === null ? '' : value) + '"' + (disabled ? ' disabled' : '') +
    ' style="width:' + escAttr(width || '110px') + ';text-align:right">';
}

function stepConstraints(plan) {
  const editable = canEditPlanInput() && !statusLocked();
  const s = state.schema;
  const dis = editable ? '' : ' disabled';

  if (!s) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">第 3 步 · 约束目标</h2></div>' +
      '<div class="card-body"><div class="empty"><b>尚未保存输入</b>' +
        '约束与求解参数来自计划的输入快照。请先回到第 2 步保存输入后再维护参数。' +
        '<div style="margin-top:10px"><a class="btn" href="#/plan/' + escAttr(String(plan.id)) + '/2">返回接口输入</a></div>' +
      '</div></div></div>';
  }

  const cal = s.calendar || {};
  const co = s.changeover || {};
  const pt = s.process_times || {};
  const tr = s.transfer_seconds || {};
  const orders = Array.isArray(s.orders) ? s.orders : [];
  const supply = Array.isArray(s.supply) ? s.supply : [];
  const models = [];
  orders.forEach(function (o) { if (o && o.model && models.indexOf(o.model) < 0) models.push(o.model); });
  const cars = orders.reduce(function (n, o) { return n + (Number(o && o.qty) > 0 ? Number(o.qty) : 1); }, 0);
  const stages = Object.keys(pt);

  const unknownKeys = Object.keys(s).filter(function (k) { return !(k in ENGINE_INPUT_SCHEMA); });

  const calendarHtml =
    '<div class="card"><div class="card-head"><h3 class="card-title">工作日历与产能（calendar）</h3>' +
      '<span class="card-sub">硬约束来源：每日可用产能</span></div><div class="card-body">' +
      '<div class="form-grid">' +
        '<div class="field"><label>工作日（day 序号，逗号分隔）</label>' +
          bindInput('schema.__workdays', Array.isArray(cal.workdays) ? cal.workdays.join(',') : '', !editable, '100%') + '</div>' +
        schemaNum('calendar.shifts_per_day', '每日班次', '1', !editable) +
        schemaNum('calendar.seconds_per_shift', '单班秒数', '1', !editable) +
        schemaNum('calendar.overtime_seconds_per_day', '每日加班秒数', '1', !editable) +
        schemaNum('calendar.max_cars_per_day', '日车辆上限（可空）', '1', !editable) +
      '</div>' +
      '<div class="hint" style="margin-top:6px">日车辆上限留空表示不限制；加班秒数计入当日可用产能。' +
        '引擎按 workdays 的序号定位计划日，序号不代表自然日期。</div>' +
    '</div></div>';

  const changeoverHtml =
    '<div class="card"><div class="card-head"><h3 class="card-title">切换规则（changeover）</h3>' +
      '<span class="card-sub">切换会占用该阶段产能</span></div><div class="card-body tight">' +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>阶段</th><th>切换键</th><th class="num">切换秒数</th></tr></thead><tbody>' +
      Object.keys(co).map(function (stage) {
        const c = co[stage] || {};
        return '<tr><td>' + esc(STAGE_FALLBACK[stage] || stage) + ' <span class="muted mono">' + esc(stage) + '</span></td>' +
          '<td>' + bindInput('schema.changeover.' + stage + '.key', c.key, !editable, '120px') + '</td>' +
          '<td class="num">' + bindNumber('schema.changeover.' + stage + '.seconds', c.seconds, !editable) + '</td>' +
          '</tr>';
      }).join('') +
      '</tbody></table></div>' +
      '<div class="hint" style="padding:0 10px 8px">切换键示例：焊装/总装用 model，涂装用 color；单位秒，0 表示不计算切换。' +
        '钢铝比例、颜色批量等规则本内核未实现。</div>' +
    '</div></div>';

  const processHtml =
    '<div class="card"><div class="card-head"><h3 class="card-title">节拍（process_times）</h3>' +
      '<span class="card-sub">默认秒数 + 按车型覆盖</span></div><div class="card-body tight">' +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>阶段</th><th class="num">默认秒数</th><th>按车型覆盖（JSON 对象）</th></tr></thead><tbody>' +
      stages.map(function (stage) {
        const p = pt[stage] || {};
        return '<tr><td>' + esc(STAGE_FALLBACK[stage] || stage) + ' <span class="muted mono">' + esc(stage) + '</span></td>' +
          '<td class="num">' + bindNumber('schema.process_times.' + stage + '.default', p.default, !editable, '110px', '0.1') + '</td>' +
          '<td>' + bindInput('processModelText.' + stage, state.processModelText[stage] || '', !editable, '100%') + '</td></tr>';
      }).join('') +
      '</tbody></table></div>' +
      '<div class="hint" style="padding:0 10px 8px">按车型覆盖留空表示全部使用默认秒数；当前订单车型：' +
        esc(models.length ? models.join('、') : '（无）') + '</div>' +
    '</div></div>';

  const transferHtml =
    '<div class="card"><div class="card-head"><h3 class="card-title">流转时间（transfer_seconds）</h3>' +
      '<span class="card-sub">焊装 → 涂装 → 总装 的流转等待</span></div><div class="card-body">' +
      '<div class="form-grid">' +
        Object.keys(tr).map(function (stage) {
          return schemaNum('transfer_seconds.' + stage, '流转到 ' + (STAGE_FALLBACK[stage] || stage) + '（秒）', '1', !editable);
        }).join('') +
      '</div>' +
    '</div></div>';

  const supplyRows = supply.map(function (x) {
    const cum = Array.isArray(x && x.cumulative) ? x.cumulative : [];
    const last = cum.length ? cum[cum.length - 1] : null;
    return '<tr><td class="mono">' + esc(x && x.material) + '</td>' +
      '<td class="num">' + esc(cum.length) + '</td>' +
      '<td class="num">' + esc(last ? last.qty : '—') + '</td>' +
      '<td class="mono">' + esc(cum.slice(-3).map(function (c) { return 'D' + c.day + ':' + c.qty; }).join('  ')) + '</td></tr>';
  }).join('');

  return '<div class="card"><div class="card-head"><h2 class="card-title">第 3 步 · 约束目标</h2>' +
      '<span class="card-sub">只维护引擎真正解析的字段</span></div>' +
    '<div class="card-body">' +
      '<div class="note warn"><b>本页字段与引擎读取的字段一一对应。</b>' +
        '引擎只解析：' + Object.keys(ENGINE_INPUT_SCHEMA).map(function (k) { return '<span class="mono">' + esc(k) + '</span>'; }).join('、') + '。' +
        '此外的自定义字段（例如自行添加的 constraints / objectives）会被引擎<b>静默忽略</b>，因此本页不再提供这类自由 JSON 输入。</div>' +
      (unknownKeys.length
        ? '<div class="note bad" style="margin-top:6px">当前输入含 ' + unknownKeys.length + ' 个引擎不识别的字段，它们不会生效：' +
          unknownKeys.map(function (k) { return '<span class="mono">' + esc(k) + '</span>'; }).join('、') +
          '。如需删除请到第 2 步编辑原始 JSON。</div>'
        : '') +
    '</div></div>' +

    '<div class="card"><div class="card-head"><h3 class="card-title">求解参数</h3>' +
      '<span class="card-sub">影响搜索过程，不改变约束口径</span></div><div class="card-body">' +
      '<div class="form-grid">' +
        schemaNum('seed', '随机种子', '1', !editable) +
        schemaNum('time_limit_seconds', '求解时限（秒，> 0）', '0.5', !editable) +
        schemaNum('drain_days', '收尾天数（>= 0）', '1', !editable) +
      '</div>' +
      '<div class="hint" style="margin-top:6px">达到时限时引擎返回“到时有解”并标注未搜索完全部候选；未找到解不代表问题无解。</div>' +
    '</div></div>' +

    calendarHtml + changeoverHtml + processHtml + transferHtml +

    '<div class="card"><div class="card-head"><h3 class="card-title">目标层级（引擎固定声明，不可修改）</h3>' +
      '<span class="card-sub">词典序最小化，越靠前越优先</span></div><div class="card-body tight">' +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th class="num">优先级</th><th>目标层级</th><th class="mono">字段</th></tr></thead><tbody>' +
      OBJECTIVE_TIERS_UI.map(function (t, i) {
        return '<tr><td class="num">' + (i + 1) + '</td><td>' + esc(t[1]) + '</td><td class="mono">' + esc(t[0]) + '</td></tr>';
      }).join('') +
      '</tbody></table></div>' +
      '<div class="note plain" style="margin:8px">目标层级与惩罚权重由引擎固定声明，界面不提供权重编辑，避免出现“改了权重但后台不执行”的假象。</div>' +
    '</div></div>' +

    '<div class="card"><div class="card-head"><h3 class="card-title">订单与物料供应（只读摘要）</h3>' +
      '<span class="card-sub">明细在步骤 2 的原始 JSON 中维护</span></div><div class="card-body">' +
      '<div class="statline" style="margin-bottom:10px">' +
        statItem('订单条数', orders.length) + statItem('车辆数（按 qty）', cars) +
        statItem('车型数', models.length) + statItem('供应物料数', supply.length) +
      '</div>' +
      (supplyRows
        ? '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>物料</th><th class="num">供应点</th><th class="num">末日累计</th><th>最后三日累计</th></tr></thead><tbody>' + supplyRows + '</tbody></table></div>'
        : '<div class="empty">输入中没有 supply 条目；引擎会把所有物料视为无限供应并给出告警。</div>') +
    '</div></div>' +

    '<div class="row" style="margin-bottom:12px">' +
      '<button type="button" class="btn cyan" data-act="saveSchema" data-id="' + escAttr(String(plan.id)) + '"' + dis + '>保存约束与参数</button>' +
      '<button type="button" class="btn" data-act="reloadSchema" data-id="' + escAttr(String(plan.id)) + '"' + dis + '>放弃修改并重载</button>' +
      '<a class="btn" href="#/plan/' + escAttr(String(plan.id)) + '/4">下一步：计算仿真</a>' +
      '<span class="hint">保存会写入输入快照并清除已有计算结果（任务历史保留）。</span>' +
    '</div>' +
    (statusLockReason() ? '<div class="note warn">' + esc(statusLockReason()) + '</div>' : '') +
    '<div class="note info">产能、切换、节拍与供应共同构成硬约束的输入；硬约束由服务端独立校验，前端不提供关闭开关。' +
      '未实现的规则（钢铝比例、颜色批次、冻结保护等）不会因这里填写而生效，详见方案分析中的引擎边界。</div>';
}

/* ---- 步骤 4：计算仿真 ---- */
function stepCompute(plan) {
  const computable = isComputable(plan.type);
  const running = taskIsRunning(state.taskDetail);
  const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];

  const taskRows = tasks.length
    ? tasks.map(function (t) {
        const isCur = String(t.id) === String(state.taskId);
        const r = taskIsRunning(t);
        return '<tr' + (isCur ? ' style="background:#f4fbfc"' : '') + '>' +
          '<td class="mono">' + esc(t.id) + (isCur ? ' ' + tagHtml('当前', 'cyan') : '') + '</td>' +
          '<td>' + tagHtml(statusLabel(t.status), r ? 'warn' : statusTone(t.status)) + '</td>' +
          '<td class="mono">' + esc(fmtTime(t.created_at)) + '</td>' +
          '<td class="row-actions"><button type="button" class="btn small" data-act="openTask" data-id="' + escAttr(String(plan.id)) + '" data-task="' + escAttr(t.id) + '">查看</button></td>' +
        '</tr>';
      }).join('')
    : '<tr><td colspan="4" class="tbl-empty">尚无计算任务</td></tr>';

  const taskPanel = state.taskId
    ? '<div class="card"><div class="card-head"><h2 class="card-title">当前任务</h2>' +
        '<span class="card-sub">任务由服务端执行，离开本页不会中断</span></div><div class="card-body">' +
        '<div class="task-head">' +
          '<span>任务 ID <b class="mono">' + esc(state.taskId) + '</b></span>' +
          '<span>状态 ' + tagHtml(statusLabel(state.taskDetail && state.taskDetail.status), running ? 'warn' : statusTone(state.taskDetail && state.taskDetail.status)) + '</span>' +
          '<span>阶段 <b>' + esc((state.taskDetail && state.taskDetail.stage) || '服务端未提供') + '</b></span>' +
          (running ? '<button type="button" class="btn small" data-act="stopPoll">停止刷新</button>'
                   : '<button type="button" class="btn small" data-act="refreshTask">刷新一次</button>') +
        '</div>' +
        (running
          ? '<div class="bar-track"><div class="bar-fill"></div></div><div class="hint">服务端未提供百分比进度，这里只显示“运行中”的动效，不代表完成度。</div>'
          : '') +
        (state.taskError ? '<div class="note bad" style="margin-top:8px">任务错误：' + esc(state.taskError) + '</div>' : '') +
        '<h3 style="margin:10px 0 6px">服务端进度日志</h3>' + progressLogHtml(state.taskDetail) +
      '</div></div>'
    : '';

  const header = '<div class="card"><div class="card-head"><h2 class="card-title">第 4 步 · 计算仿真</h2>' +
      '<span class="card-sub">' + esc(typeLabel(plan.type)) + (computable ? ' · 真实计算' : ' · 计算未实现') + '</span></div>' +
    '<div class="card-body">' +
      (computable
        ? '<div class="note warn"><b>研究模型警示：</b>该计划类型已接入后端真实计算，但内核是研究模型——' +
          '未实现人工冻结、冻结区与已投产保护，也未实现完整原文工艺规则。' +
          '<b>请勿输入真实已投产或冻结订单</b>，结果不得用于实际生产决策，平台也不会向 ERP / MES 下发。</div>'
        : '<div class="note warn"><b>该计划类型的计算尚未实现。</b>' + esc(typeNote(plan.type)) +
          ' 若需要 W+3 的真实计算，请返回列表新建 W+3 计划。</div>') +
      '<div class="row" style="margin-top:12px">' +
        '<button type="button" class="btn primary" data-act="runCompute" data-id="' + escAttr(String(plan.id)) + '"' +
          (computable && canRunCompute() && !statusLocked() && !running ? '' : ' disabled') + '>' +
          (running ? '计算进行中…' : '发起计算') + '</button>' +
        '<a class="btn" href="#/plan/' + escAttr(String(plan.id)) + '/5">查看方案分析</a>' +
        '<button type="button" class="btn" data-act="showDbRecords" data-id="' + escAttr(String(plan.id)) + '">核验数据库记录</button>' +
        (!computable ? '<span class="hint">按钮已禁用：未实现的计算不会返回结果。</span>' : '') +
        (computable && !canRunCompute() ? '<span class="hint">当前角色不显示计算入口（服务端仍会校验）。</span>' : '') +
      '</div>' +
    '</div></div>';

  const history = '<div class="card"><div class="card-head"><h2 class="card-title">任务历史</h2>' +
      '<span class="card-sub">共 ' + tasks.length + ' 条</span></div>' +
    '<div class="card-body tight"><div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr><th>任务 ID</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>' +
      '<tbody>' + taskRows + '</tbody></table></div></div></div>';

  return header + taskPanel + history;
}

function progressLogHtml(task) {
  const log = task && Array.isArray(task.progress_log) ? task.progress_log : [];
  if (!log.length) return '<div class="empty">服务端暂未返回进度日志</div>';
  return '<ul class="task-log">' + log.map(function (line) {
    if (line && typeof line === 'object') {
      const t = line.time || line.at || line.timestamp || '';
      const msg = line.message || line.msg || line.text || line.stage || safeJson(line);
      return '<li>' + (t ? '<span class="lg-time">' + esc(t) + '</span>' : '') + esc(msg) + '</li>';
    }
    return '<li>' + esc(line) + '</li>';
  }).join('') + '</ul>';
}

/* ---- 步骤 5：方案分析 ---- */
function stepAnalysis(plan) {
  if (!state.rawResult) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">第 5 步 · 方案分析</h2></div>' +
      '<div class="card-body"><div class="empty"><b>尚无计算结果</b>' +
        (isComputable(plan.type)
          ? '请先在“计算仿真”步骤发起计算并等待完成。'
          : esc(typeNote(plan.type)) + ' 因此本页不会显示方案对比。') +
        '<div style="margin-top:10px"><a class="btn" href="#/plan/' + escAttr(String(plan.id)) + '/4">前往计算仿真</a></div>' +
        '</div></div></div>';
  }

  if (!state.result) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">第 5 步 · 方案分析</h2>' +
        '<span class="card-sub">结果结构尚未接入</span></div>' +
      '<div class="card-body">' +
        '<div class="note warn">后端返回了结果，但 normalizeResult() 尚不能识别其结构。' +
          '为避免显示错误指标，界面在此不做任何推断。请主会话补充 normalizeResult 的字段映射后，本页会自动渲染基线对比、事件甘特、指标、校验与未排原因。</div>' +
        '<details class="raw" open><summary>原始结果 JSON</summary>' + rawPreviewHtml(state.rawResult) + '</details>' +
      '</div></div>';
  }

  const r = state.result;
  const cands = r.candidates;
  const opt = cands.filter(function (c) { return c.key === 'optimized'; })[0] || cands[cands.length - 1];

  const picker = '<div class="row" style="margin-bottom:10px"><span class="hint">选择用于提交的方案：</span>' +
    cands.map(function (c) {
      return '<label class="row" style="gap:4px"><input type="radio" name="cand" value="' + escAttr(c.key) + '" data-act="pickCandidate"' +
        (state.selectedCandidate === c.key ? ' checked' : '') + '> ' + esc(c.label) +
        (c.sublabel ? ' <span class="muted">' + esc(c.sublabel) + '</span>' : '') + '</label>';
    }).join('') + '</div>';

  const warn = cands.length < 2
    ? '<div class="note warn">后端本次只返回了 1 个候选方案，无法做基线对比；界面不会自行构造基线的任何数值。</div>'
    : '';

  const blockNote = (opt && !opt.submittable)
    ? '<div class="note bad"><b>当前选中的优化方案不可提交。</b>引擎给出的阻断项：' +
      '<ul style="margin:4px 0 0;padding-left:18px">' +
      (opt.submitBlockers || []).map(function (b) { return '<li>' + esc(b) + '</li>'; }).join('') +
      '</ul></div>'
    : '';

  const research = '<div class="note bad"><b>研究模型结果，不得用于实际生产决策。</b>' +
    '本内核未实现人工冻结、冻结区与已投产保护，也未实现钢铝比例、颜色批次、缓存重排等完整原文工艺；' +
    '因此结果中不包含任何“已投产/冻结订单受保护”的结论。请勿把真实已投产或冻结订单作为输入用于验证。</div>';

  const metricCount = metricUnion(cands).length;

  return research +
    '<div class="card"><div class="card-head"><h2 class="card-title">第 5 步 · 方案分析</h2>' +
      '<span class="card-sub">优化方案核心指标 · 数值原样来自服务端</span></div>' +
    '<div class="card-body">' + warn + blockNote + picker + coreMetricBlock(opt) +
      '<div class="hint" style="margin-top:6px">“不适用”表示该指标在服务端存在但为空（例如无迟单时的平均延期）；“未提供”表示结果中没有该字段。</div>' +
    '</div></div>' +
    renderGanttCard(opt, cands) +
    '<div class="card"><div class="card-head"><h2 class="card-title">详细指标与目标层级</h2>' +
      '<span class="card-sub">共 ' + metricCount + ' 项指标</span></div>' +
      '<div class="card-body">' +
        fold('展开：基线与优化方案指标对比（' + metricCount + ' 项）', renderMetricCompare(cands)) +
        fold('展开：目标层级逐层对比', renderTierCompareInner(r)) +
      '</div></div>' +
    renderDayLoadCard(opt) +
    renderSequenceCard(opt) +
    renderValidationCard(cands) +
    renderUnscheduledCard(cands) +
    renderSearchCandidatesCard(r) +
    renderEngineScopeCard(r) +
    renderAssistantCard(r) +
    '<div class="card"><div class="card-head"><h2 class="card-title">原始结果</h2>' +
      '<span class="card-sub">便于核对归一化是否丢字段</span></div>' +
      '<div class="card-body"><details class="raw"><summary>查看原始 JSON</summary>' + rawPreviewHtml(state.rawResult) + '</details></div></div>';
}


function metricUnion(cands) {
  const map = {};
  const order = [];
  cands.forEach(function (c) {
    (c.metrics || []).forEach(function (m) {
      const k = m.key || m.label;
      if (!map[k]) { map[k] = { key: k, label: m.label || k, unit: m.unit || '', note: m.note || '', values: {} }; order.push(k); }
      map[k].values[c.key] = m;
    });
  });
  return order.map(function (k) { return map[k]; });
}

function numOrNull(v) {
  if (typeof v === 'number' && isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '' && !isNaN(Number(v.replace(/,/g, '')))) return Number(v.replace(/,/g, ''));
  return null;
}

function fmtNum(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return v.toLocaleString('zh-CN');
  return String(v);
}

function fmtMetricValue(m) {
  if (!m || m.value === undefined) return '未提供';
  if (m.value === null) return '不适用';
  const v = m.value;
  if (typeof v === 'number') {
    if (m.fmt === 'rate') return (v * 100).toFixed(1) + '%';
    if (m.fmt === 'int') return v.toLocaleString('zh-CN');
    return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }
  return String(v);
}

/** 步骤 5 首屏核心指标：全部来自服务端，缺少字段时明确显示“未提供”。 */
const CORE_METRICS = [
  ['completed_count', '已排/完工车辆', '台'],
  ['on_time_rate', '按期完工率', '%'],
  ['total_blocked_min', '正阻塞时长', '分'],
  ['color_switches', '涂装换色次数', '次'],
  ['makespan_min', '完工跨度', '分']
];

function coreMetricBlock(cand) {
  const items = CORE_METRICS.map(function (d) {
    const m = (cand.metrics || []).filter(function (x) { return x.key === d[0]; })[0];
    let text = '未提供';
    if (m) {
      if (m.value === null) text = '不适用';
      else if (d[0] === 'on_time_rate') text = (m.value * 100).toFixed(1) + '%';
      else if (d[0] === 'total_blocked_min' || d[0] === 'makespan_min') text = fmtNum(m.value) + ' ' + d[2];
      else if (typeof m.value === 'number') text = m.value.toLocaleString('zh-CN') + (d[2] ? ' ' + d[2] : '');
      else text = String(m.value);
    }
    return '<div class="item"><span class="k">' + esc(d[1]) + '</span>' +
      '<span class="v">' + esc(text) + '</span></div>';
  }).join('');

  const vs = cand.validationSummary || { passed: false, total: 0, failed: 0 };
  const checkItem = '<div class="item"><span class="k">独立模型校验</span><span class="v">' +
    (vs.total
      ? (vs.passed ? tagHtml('全部通过（' + vs.total + ' 项）', 'ok') : tagHtml(vs.failed + '/' + vs.total + ' 未通过', 'bad'))
      : '<span class="tag warn">结果未提供</span>') +
    '</span></div>';

  return '<div class="statline">' + items + checkItem + '</div>';
}

function fold(summaryText, inner, open) {
  return '<details class="fold"' + (open ? ' open' : '') + '><summary>' + esc(summaryText) + '</summary>' +
    '<div style="margin-top:6px">' + inner + '</div></details>';
}

function renderMetricCompare(cands) {
  const rows = metricUnion(cands);
  if (!rows.length) {
    return '<div class="empty">结果中未包含指标字段。界面不显示任何示例数值。</div>';
  }
  const cols = cands;
  const th = '<tr><th>指标</th>' + cols.map(function (c) { return '<th class="num">' + esc(c.label) + '</th>'; }).join('') +
    (cols.length > 1 ? '<th class="num">优化 − 基线</th>' : '') + '</tr>';

  const base = cols.filter(function (c) { return c.key === 'baseline'; })[0];
  const opt = cols.filter(function (c) { return c.key === 'optimized'; })[0];

  const body = rows.map(function (row) {
    let diff = '—';
    if (base && opt) {
      const bm = row.values['baseline'], om = row.values['optimized'];
      const bn = bm ? numOrNull(bm.value) : null;
      const on = om ? numOrNull(om.value) : null;
      if (bn !== null && on !== null) {
        const d = on - bn;
        const shown = (bm.fmt === 'rate') ? (d * 100).toFixed(1) + ' 个百分点' : (d > 0 ? '+' : '') + d.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
        diff = shown;
      }
    }
    return '<tr><td>' + esc(row.label) + (row.unit ? ' <span class="muted">(' + esc(row.unit) + ')</span>' : '') + '</td>' +
      cols.map(function (c) {
        const m = row.values[c.key];
        return '<td class="num">' + esc(fmtMetricValue(m)) + '</td>';
      }).join('') +
      (cols.length > 1 ? '<td class="num mono">' + esc(diff) + '</td>' : '') + '</tr>';
  }).join('');

  return '<div class="tbl-wrap"><table class="tbl"><thead>' + th + '</thead><tbody>' + body + '</tbody></table></div>' +
    '<div class="hint" style="margin-top:6px">指标原样取自服务端返回；差值由界面按两个候选的数值直接相减得到，比率差以百分点表示。</div>';
}

function renderTierCompareInner(result) {
  const tiers = result.objectiveTiers || [];
  if (!tiers.length) {
    return '<div class="empty">结果中未包含目标层级（comparison.objective_tiers）。</div>';
  }
  return '<div class="hint" style="margin-bottom:6px">按声明的词典序逐层比较，越靠前越优先。</div>' +
    '<div class="tbl-wrap"><table class="tbl">' +
    '<thead><tr><th>优先级</th><th>目标层级</th><th class="num">基线</th><th class="num">优化</th><th class="num">差值</th><th>改善</th></tr></thead><tbody>' +
    tiers.map(function (t, i) {
      const d = (t.delta === null || t.delta === undefined) ? '—' : ((t.delta > 0 ? '+' : '') + t.delta.toLocaleString('zh-CN'));
      return '<tr><td>' + (i + 1) + '</td><td>' + esc(t.label) + '</td>' +
        '<td class="num">' + esc(t.baseline === undefined ? '—' : t.baseline) + '</td>' +
        '<td class="num">' + esc(t.optimized === undefined ? '—' : t.optimized) + '</td>' +
        '<td class="num mono">' + esc(d) + '</td>' +
        '<td>' + (t.improved ? tagHtml('改善', 'ok') : tagHtml('未改善', 'plain')) + '</td></tr>';
    }).join('') + '</tbody></table></div>';
}

function fmtRate(v) {
  return (v === null || v === undefined || isNaN(Number(v))) ? '—' : (Number(v) * 100).toFixed(1) + '%';
}

function fmtMinutes(v) {
  return (v === null || v === undefined || isNaN(Number(v))) ? '—' : Math.round(Number(v) / 60).toLocaleString('zh-CN');
}

function fmtInt(v) {
  return (v === null || v === undefined || isNaN(Number(v))) ? '—' : Number(v).toLocaleString('zh-CN');
}

function renderDayLoadCard(cand) {
  const rows = cand ? cand.dayLoads : null;
  if (!rows || !rows.length) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">分日负荷</h2></div>' +
      '<div class="card-body"><div class="empty">结果中未包含 schedule.days 分日数据。</div></div></div>';
  }
  return '<div class="card"><div class="card-head"><h2 class="card-title">分日负荷</h2>' +
      '<span class="card-sub">' + esc(cand.label) + ' · 来自 schedule.days</span></div>' +
      '<div class="card-body">' +
      fold('展开分日负荷明细（' + rows.length + ' 天）',
        '<div class="tbl-wrap"><table class="tbl">' +
        '<thead><tr><th>计划日</th><th>工作日</th><th class="num">排产车辆</th><th class="num">焊装计划(分)</th>' +
        '<th class="num">焊装产能(分)</th><th class="num">焊装利用率</th><th class="num">涂装利用率</th><th class="num">总装利用率</th><th>上日末车型</th></tr></thead><tbody>' +
        rows.map(function (d) {
          return '<tr><td>D' + esc(d.day) + '</td>' +
            '<td>' + (d.isWorkday ? tagHtml('工作日', 'plain') : tagHtml('非工作日', 'warn')) + '</td>' +
            '<td class="num">' + esc(fmtInt(d.assignedCars)) + '</td>' +
            '<td class="num">' + esc(fmtMinutes(d.weldTotal)) + '</td>' +
            '<td class="num">' + esc(fmtMinutes(d.weldCapacity)) + '</td>' +
            '<td class="num">' + esc(fmtRate(d.weldUtil)) + '</td>' +
            '<td class="num">' + esc(fmtRate(d.paintUtil)) + '</td>' +
            '<td class="num">' + esc(fmtRate(d.assemblyUtil)) + '</td>' +
            '<td class="mono">' + esc(d.carryIn || '—') + '</td></tr>';
        }).join('') + '</tbody></table></div>') +
      '<div class="hint" style="margin-top:6px">利用率与产能为服务端原值，此处换算为分钟显示；非工作日产能为 0。</div></div></div>';
}


function renderSequenceCard(cand) {
  const rows = cand ? cand.sequence : null;
  if (!rows || !rows.length) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">逐车序位明细</h2></div>' +
      '<div class="card-body"><div class="empty">结果中未包含 schedule.sequence 车序数据。</div></div></div>';
  }
  const LIMIT = 30;
  const shown = rows.slice(0, LIMIT);
  const late = rows.filter(function (r) { return (r.lateDays || 0) > 0; });
  return '<div class="card"><div class="card-head"><h2 class="card-title">逐车序位明细</h2>' +
      '<span class="card-sub">' + esc(cand.label) + ' · 共 ' + rows.length + ' 台，此处显示前 ' + shown.length +
      ' 台' + (late.length ? ' · 延期 ' + late.length + ' 台' : '') + '</span></div>' +
    '<div class="card-body">' +
      fold('展开逐车序位（共 ' + rows.length + ' 台，显示前 ' + shown.length + ' 台）',
      '<div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr><th class="num">序位</th><th>计划日</th><th>车辆</th><th>订单</th><th>车型</th><th>颜色</th>' +
      '<th class="num">需求日</th><th class="num">延期(天)</th></tr></thead><tbody>' +
      shown.map(function (r) {
        return '<tr><td class="num">' + esc(r.position) + '</td><td>D' + esc(r.day) + '</td>' +
          '<td class="mono">' + esc(r.carId) + '</td><td class="mono">' + esc(r.orderId) + '</td>' +
          '<td>' + esc(r.model) + '</td><td>' + esc(r.color) + '</td>' +
          '<td class="num">D' + esc(r.dueDay) + '</td>' +
          '<td class="num">' + ((r.lateDays || 0) > 0 ? tagHtml(String(r.lateDays), 'warn') : '0') + '</td></tr>';
      }).join('') + '</tbody></table></div>') +
      '<div class="hint" style="margin-top:6px">完整 ' + rows.length + ' 台明细见原始结果 JSON；界面不做截断以外的加工。</div></div></div>';
}

function renderSearchCandidatesCard(result) {
  const list = result.searchCandidates || [];
  if (!list.length) return '';
  const submittable = list.filter(function (c) { return c.submittable; }).length;
  return '<div class="card"><div class="card-head"><h2 class="card-title">搜索候选明细</h2>' +
      '<span class="card-sub">共评估 ' + list.length + ' 个候选 · 可提交 ' + submittable + ' 个</span></div>' +
    '<div class="card-body">' +
      fold('展开 ' + list.length + ' 个候选明细（可提交 ' + submittable + ' 个）',
      '<div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr><th>候选</th><th>策略</th><th class="num">词典序向量</th><th class="num">加权罚分</th>' +
      '<th class="num">已排</th><th class="num">未排</th><th class="num">延期车</th><th class="num">延期天</th>' +
      '<th class="num">切换(秒)</th><th class="num">跨度(天)</th><th>校验</th><th>可提交</th></tr></thead><tbody>' +
      list.map(function (c) {
        return '<tr><td class="mono">' + esc(c.name) + '</td><td class="mono">' + esc(c.strategy || '—') + '</td>' +
          '<td class="num mono">' + esc(c.objective || '—') + '</td>' +
          '<td class="num">' + esc(c.weightedPenalty === undefined ? '—' : c.weightedPenalty) + '</td>' +
          '<td class="num">' + esc(fmtInt(c.scheduledCars)) + '</td>' +
          '<td class="num">' + esc(fmtInt(c.unscheduledCars)) + '</td>' +
          '<td class="num">' + esc(fmtInt(c.lateCars)) + '</td>' +
          '<td class="num">' + esc(fmtInt(c.tardinessDays)) + '</td>' +
          '<td class="num">' + esc(fmtInt(c.changeoverSeconds)) + '</td>' +
          '<td class="num">' + esc(fmtInt(c.flowSpanDays)) + '</td>' +
          '<td>' + (c.validationPassed === null ? '—' : (c.validationPassed ? tagHtml('通过', 'ok') : tagHtml('未通过', 'bad'))) + '</td>' +
          '<td>' + (c.submittable ? tagHtml('可提交', 'ok') : tagHtml('不可提交', 'warn')) + '</td></tr>';
      }).join('') + '</tbody></table></div>') +
      '<div class="hint" style="margin-top:6px">这些是求解器实际评估过的候选；提交时服务端只接受 baseline 与 optimized。</div></div></div>';
}

const RAW_PREVIEW_LIMIT = 120000;

/** 完整结果可达 1MB 以上；内联预览截断，完整数据用下载按钮获取，避免拖慢页面。 */
function rawPreviewHtml(obj) {
  const s = safeJson(obj);
  const sizeKb = Math.max(1, Math.round(s.length / 1024));
  const truncated = s.length > RAW_PREVIEW_LIMIT;
  return '<div class="row" style="margin-bottom:6px">' +
      '<button type="button" class="btn small" data-act="downloadRaw">下载完整结果 JSON（约 ' + sizeKb + ' KB）</button>' +
      (truncated
        ? '<span class="tag warn">预览已截断</span><span class="hint">仅显示前 ' + Math.round(RAW_PREVIEW_LIMIT / 1024) + ' KB，完整内容请下载</span>'
        : '<span class="tag plain">完整预览</span>') +
    '</div>' +
    '<pre>' + esc(truncated ? s.slice(0, RAW_PREVIEW_LIMIT) : s) + '</pre>';
}

function renderEngineScopeCard(result) {  const meta = result.meta || {};
  const list = function (arr) {
    return arr && arr.length
      ? '<ul style="margin:4px 0 0;padding-left:18px;color:#3c556b">' + arr.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>'
      : '<div class="hint">结果未提供该声明。</div>';
  };
  const runtime = meta.runtimeStatus
    ? (meta.runtimeStatus + (meta.runtimeSeconds !== undefined ? ' · ' + meta.runtimeSeconds + ' 秒' : '') +
      (meta.candidatesEvaluated !== undefined ? ' · 评估 ' + meta.candidatesEvaluated + ' 个候选' : '') +
      (meta.candidateBudget !== undefined ? '（预算 ' + meta.candidateBudget + '）' : ''))
    : '结果未提供运行信息';

  return '<div class="card"><div class="card-head"><h2 class="card-title">引擎声明与边界</h2>' +
      '<span class="card-sub">来自结果的 rule_scope / assumptions / caveats，未经界面改写</span></div>' +
    '<div class="card-body">' +
      '<div class="kv" style="margin-bottom:10px">' +
        '<div><span class="k">引擎版本</span><span class="v mono">' + esc(meta.engineVersion || '—') + '</span></div>' +
        '<div><span class="k">输入契约</span><span class="v mono">' + esc(meta.inputContractVersion || '—') + '</span></div>' +
        '<div><span class="k">随机种子</span><span class="v mono">' + esc(meta.seed === undefined ? '—' : meta.seed) + '</span></div>' +
        '<div><span class="k">求解时限</span><span class="v">' + esc(meta.timeLimit === undefined ? '—' : meta.timeLimit + ' 秒') + '</span></div>' +
        '<div><span class="k">运行</span><span class="v">' + esc(runtime) + '</span></div>' +
      '</div>' +
      fold('展开：已实现 / 未实现规则、前提假设与已知局限',
        '<div class="split">' +
          '<div><h3>已实现规则</h3>' + list(meta.implemented) + '</div>' +
          '<div><h3>未实现规则（不得视为已满足）</h3>' + list(meta.notImplemented) + '</div>' +
        '</div>' +
        '<div class="split" style="margin-top:12px">' +
          '<div><h3>前提假设</h3>' + list(meta.assumptions) + '</div>' +
          '<div><h3>已知局限</h3>' + list(meta.caveats) + '</div>' +
        '</div>' +
        (meta.warnings && meta.warnings.length ? '<div class="note warn" style="margin-top:10px"><b>输入告警</b>' + list(meta.warnings) + '</div>' : '')
      ) +
      '<div class="note bad" style="margin-top:10px">本引擎的校验是<b>独立模型校验，不代表全厂完整工艺规则</b>；' +
        '人工冻结、冻结区与已投产保护未实现，界面不会把未实现的约束显示为已满足。</div>' +
    '</div></div>';
}

function renderGanttCard(opt, cands) {
  const g = opt && opt.gantt;
  if (!g) {
    const any = cands.filter(function (c) { return c.gantt; });
    if (!any.length) {
      return '<div class="card"><div class="card-head"><h2 class="card-title">事件甘特</h2><span class="card-sub">HTML/CSS 实现，无外部图表库</span></div>' +
        '<div class="card-body"><div class="empty">结果中未包含甘特数据（gantt.resources / gantt.buckets / gantt.events）。界面不绘制示例事件。</div></div></div>';
    }
  }
  const data = g || (cands.filter(function (c) { return c.gantt; })[0] || {}).gantt;
  if (!data) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">事件甘特</h2></div>' +
      '<div class="card-body"><div class="empty">未选择到包含甘特数据的方案。</div></div></div>';
  }

  const cols = ['minmax(110px, 160px)'].concat(data.buckets.map(function () { return 'minmax(64px, 1fr)'; })).join(' ');
  const head = '<div class="gcell ghead growhead">资源 / 时间</div>' +
    data.buckets.map(function (b) { return '<div class="gcell ghead">' + esc(b.label) + '</div>'; }).join('');

  const rows = data.resources.map(function (res) {
    let cells = '<div class="gcell growhead">' + esc(res.label) + (res.sub ? '<small>' + esc(res.sub) + '</small>' : '') + '</div>';
    cells += data.buckets.map(function (b) {
      const evs = data.events.filter(function (e) { return e.resourceKey === res.key && e.bucketKey === b.key; });
      const inner = evs.map(function (e) {
        const tone = ['ok', 'warn', 'bad', 'cyan', 'plain'].indexOf(String(e.tone)) >= 0 ? e.tone : '';
        return '<span class="gbar ' + escAttr(tone) + '" title="' + escAttr((e.label || '') + (e.sublabel ? ' · ' + e.sublabel : '')) + '">' +
          esc(e.label) + (e.sublabel ? ' · ' + esc(e.sublabel) : '') + '</span>';
      }).join('');
      return '<div class="gcell">' + inner + '</div>';
    }).join('');
    return cells;
  }).join('');

  return '<div class="card"><div class="card-head"><h2 class="card-title">事件甘特</h2>' +
      '<span class="card-sub">' + esc(opt ? opt.label : '') + ' · ' + data.resources.length + ' 资源 × ' + data.buckets.length + ' 时间桶 · ' + data.events.length + ' 事件</span></div>' +
    '<div class="card-body tight" style="padding:10px">' +
      '<div class="gantt"><div class="gantt-grid" style="grid-template-columns:' + esc(cols) + '">' + head + rows + '</div></div>' +
      '<div class="hint" style="margin-top:6px">纯 HTML/CSS 渲染，未引入任何图表库；时间段与资源含义由服务端结果定义。</div>' +
    '</div></div>';
}

function renderValidationCard(cands) {
  const rows = [];
  cands.forEach(function (c) {
    (c.validation || []).forEach(function (v) { rows.push({ cand: c, v: v }); });
  });
  if (!rows.length) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">统一校验</h2></div>' +
      '<div class="card-body"><div class="empty">结果中未包含校验数据。界面不假设“校验通过”。</div></div></div>';
  }
  const toneOf = function (l) { return l === 'pass' ? 'ok' : l === 'warn' ? 'warn' : l === 'fail' ? 'bad' : 'plain'; };

  const summary = cands.map(function (c) {
    const s = c.validationSummary || { passed: false, total: 0, failed: 0 };
    return '<span>' + esc(c.label) + '：' +
      (s.passed ? tagHtml('全部通过', 'ok') : tagHtml(s.failed + ' 项未通过', 'bad')) +
      ' <span class="muted">共 ' + s.total + ' 项</span></span>';
  }).join('　');

  const failed = rows.filter(function (r) { return r.v.level === 'fail'; });

  return '<div class="card"><div class="card-head"><h2 class="card-title">统一校验</h2>' +
      '<span class="card-sub">由服务端独立复算，不采信求解过程自报成功</span></div>' +
    '<div class="card-body tight">' +
      '<div class="note warn" style="margin:8px">该校验为<b>独立模型校验，不代表全厂完整工艺规则</b>：' +
        '未实现的约束不会因这里显示“通过”而自动满足，详见下方“引擎声明与边界”。</div>' +
      '<div style="padding:0 8px 8px">' + summary + '</div>' +
      (failed.length
        ? '<div class="note bad" style="margin:0 8px 8px">未通过项：' +
          failed.map(function (f) { return esc(f.v.item) + '（' + esc(f.cand.label) + '）'; }).join('；') + '</div>'
        : '<div class="note ok" style="margin:0 8px 8px">没有未通过项；提交仍以服务端校验为准。</div>') +
      '<div class="tbl-wrap"><table class="tbl">' +
      '<thead><tr><th>方案</th><th>校验项</th><th>结果</th><th>说明</th></tr></thead><tbody>' +
      rows.map(function (row) {
        return '<tr><td>' + esc(row.cand.label) + '</td><td class="mono">' + esc(row.v.item) + '</td>' +
          '<td>' + tagHtml(row.v.level === 'pass' ? '通过' : '未通过', toneOf(row.v.level)) + '</td>' +
          '<td>' + esc(row.v.detail || '—') + '</td></tr>';
      }).join('') + '</tbody></table></div></div></div>';
}

function renderUnscheduledCard(cands) {
  const rows = [];
  cands.forEach(function (c) {
    (c.unscheduled || []).forEach(function (u) { rows.push({ cand: c, u: u }); });
  });
  if (!rows.length) {
    return '<div class="card"><div class="card-head"><h2 class="card-title">未排订单与原因</h2></div>' +
      '<div class="card-body"><div class="empty">结果中未包含未排明细。' +
        '不能据此认为“全部已排”；以服务端返回字段为准。</div></div></div>';
  }
  const byReason = {};
  rows.forEach(function (r) { const k = r.u.reason || '未说明'; byReason[k] = (byReason[k] || 0) + 1; });
  const summary = Object.keys(byReason).map(function (k) { return esc(k) + '（' + byReason[k] + '）'; }).join('；');

  return '<div class="card"><div class="card-head"><h2 class="card-title">未排订单与原因</h2>' +
      '<span class="card-sub">共 ' + rows.length + ' 条</span></div>' +
    '<div class="card-body">' +
      '<div class="note warn">原因分布：' + summary + '</div>' +
      '<div class="tbl-wrap" style="margin-top:8px"><table class="tbl">' +
      '<thead><tr><th>方案</th><th>车辆/订单</th><th>车型与日期</th><th>原因</th><th>引擎明细</th></tr></thead><tbody>' +
      rows.map(function (r) {
        return '<tr><td>' + esc(r.cand.label) + '</td><td class="mono">' + esc(r.u.order) + '</td>' +
          '<td>' + esc(r.u.scope || '—') + '</td><td>' + esc(r.u.reason) + '</td>' +
          '<td class="mono">' + esc(r.u.detail || '—') + '</td></tr>';
      }).join('') + '</tbody></table></div></div></div>';
}

/* ---- 确定性说明助手（本地规则式，未接入大模型） ---- */
function renderAssistantCard(result) {
  const presets = ['为什么存在未排订单？', '优化方案与基线有什么差异？', '校验是否全部通过？', '本次结果包含哪些事件？'];
  return '<div class="card assistant"><div class="card-head"><h2 class="card-title">方案说明助手</h2>' +
      '<span class="card-sub">确定性指标说明 · 未接入大模型</span></div>' +
    '<div class="card-body">' +
      '<div class="note info">本助手按固定规则从本次计算结果字段中取数作答，属于<b>确定性指标说明，不是大模型分析</b>，' +
        '未接入任何大语言模型；不能推理、不会补充结果之外的信息，缺失字段会直接说明“未提供”。</div>' +
      (result.explanation && result.explanation.summary
        ? '<div class="qa" style="border-top:0"><div class="q">服务端结果说明</div><div class="a">' + esc(result.explanation.summary) + '</div></div>'
        : '') +
      '<div class="qqa" style="margin-top:10px">' +
        presets.map(function (q) {
          return '<button type="button" class="btn small" data-act="askPreset" data-q="' + escAttr(q) + '">' + esc(q) + '</button>';
        }).join('') +
      '</div>' +
      '<div class="row"><input type="text" id="assistantQ" placeholder="也可以输入关键词，例如：未排、基线、校验、事件" style="min-width:320px">' +
        '<button type="button" class="btn cyan" data-act="askAssistant">提问</button></div>' +
      '<div style="margin-top:10px">' +
        (state.assistantLog.length
          ? state.assistantLog.map(function (qa) {
              return '<div class="qa"><div class="q">' + esc(qa.q) + '</div><div class="a">' + qa.a + '</div></div>';
            }).join('')
          : '<div class="hint">尚无提问记录。</div>') +
      '</div>' +
    '</div></div>';
}

function assistantAnswer(question) {
  const q = String(question || '').trim();
  const r = state.result;
  if (!q) return '请输入问题或点击上面的示例问题。';
  if (!r || !r.candidates || !r.candidates.length) {
    return '当前没有可用的归一化计算结果，无法作答。请先在“计算仿真”步骤运行计算并等待任务完成。';
  }

  const cands = r.candidates;
  const opt = cands.filter(function (c) { return c.key === 'optimized'; })[0] || cands[cands.length - 1];
  const base = cands.filter(function (c) { return c.key === 'baseline'; })[0];

  if (/未排|排不进|未满足/.test(q)) {
    const list = opt.unscheduled || [];
    if (!list.length) return '本次结果中，' + esc(opt.label) + ' 的未排明文字段为空；但字段为空不等于“全部已排”，需以服务端结果为准。';
    const by = {};
    list.forEach(function (u) { by[u.reason] = (by[u.reason] || 0) + 1; });
    const lines = Object.keys(by).map(function (k) { return '<li>' + esc(k) + '：' + by[k] + ' 条</li>'; }).join('');
    const top = list.slice(0, 5).map(function (u) {
      return '<li>' + esc(u.order) + ' — ' + esc(u.reason) + (u.scope ? '（' + esc(u.scope) + '）' : '') + '</li>';
    }).join('');
    return '共 ' + list.length + ' 条未排记录，原因分布：<ul>' + lines + '</ul>前 ' + Math.min(5, list.length) + ' 条明细：<ul>' + top + '</ul>';
  }

  if (/基线|差异|对比|区别/.test(q)) {
    if (!base) return '本次结果只包含 ' + cands.length + ' 个候选，没有基线方案字段，无法做基线对比；界面不会自行构造基线数值。';
    const rows = metricUnion(cands).map(function (row) {
      const bm = row.values['baseline'], om = row.values[opt.key];
      if (!bm && !om) return '';
      const bn = bm ? numOrNull(bm.value) : null;
      const on = om ? numOrNull(om.value) : null;
      let d = '未提供';
      if (bn !== null && on !== null) d = (on - bn > 0 ? '+' : '') + (on - bn).toLocaleString('zh-CN');
      return '<li>' + esc(row.label) + '：基线 ' + esc(bm ? fmtNum(bm.value) : '未提供') + ' → ' + esc(om ? fmtNum(om.value) : '未提供') + '（差 ' + esc(d) + '）</li>';
    }).filter(Boolean).join('');
    if (!rows) return '基线与优化方案都未提供可比较的指标字段。';
    return '按服务端指标字段逐项比较（差值由界面直接相减）：<ul>' + rows + '</ul>';
  }

  if (/校验|约束|合规|可行/.test(q)) {
    const vs = opt.validation || [];
    if (!vs.length) return '本次结果未包含校验数据，无法判断是否通过；界面不会假设“校验通过”。';
    const c = { pass: 0, warn: 0, fail: 0, info: 0 };
    vs.forEach(function (v) { c[v.level] = (c[v.level] || 0) + 1; });
    const fails = vs.filter(function (v) { return v.level === 'fail'; });
    return '共 ' + vs.length + ' 项校验：通过 ' + c.pass + '、警告 ' + c.warn + '、未通过 ' + c.fail + '、信息 ' + c.info + '。' +
      (fails.length ? '<ul>' + fails.map(function (f) { return '<li>' + esc(f.item) + '：' + esc(f.detail || '无说明') + '</li>'; }).join('') + '</ul>'
                    : '未返回未通过项；最终可行性仍以服务端统一校验为准。');
  }

  if (/事件|甘特|排产|时间/.test(q)) {
    const g = opt.gantt;
    if (!g) return '本次结果未包含甘特事件数据，无法列出事件。';
    const byRes = {};
    g.events.forEach(function (e) { byRes[e.resourceKey] = (byRes[e.resourceKey] || 0) + 1; });
    return '共 ' + g.resources.length + ' 个资源、' + g.buckets.length + ' 个时间桶、' + g.events.length + ' 个事件。<ul>' +
      g.resources.map(function (res) { return '<li>' + esc(res.label) + '：' + (byRes[res.key] || 0) + ' 个事件</li>'; }).join('') + '</ul>';
  }

  if (/指标|数据|多少|统计/.test(q)) {
    const rows = metricUnion(cands);
    if (!rows.length) return '本次结果未包含指标字段。';
    const bm = metricUnion(cands).map(function (row) {
      const m = row.values[opt.key];
      return m ? '<li>' + esc(row.label) + '：' + esc(fmtMetricValue(m)) + (row.unit ? ' ' + esc(row.unit) : '') + '</li>' : '';
    }).filter(Boolean).join('');
    return esc(opt.label) + ' 可用指标：<ul>' + bm + '</ul>';
  }

  if (/提交|下一步|流程/.test(q)) {
    return '当前计划状态为 ' + esc(statusLabel(state.plan && state.plan.status)) +
      '，界面选定的提交方案是 ' + esc(state.selectedCandidate || '未选择') +
      '。可在“确认提交”步骤提交；批准需切换到生产主管身份在“待办审批”执行。' +
      '注意：演示环境不接入生产系统，提交与批准都不会产生任何生产指令。';
  }

  return '本地确定性助手只回答与本次计算结果字段相关的问题。可用的关键词：未排、基线/差异、校验/约束、事件/甘特、指标、提交/流程。' +
    '本助手未接入大模型，无法回答结果之外的业务问题。';
}

/* ---- 步骤 6：确认提交 ---- */
function stepSubmit(plan) {
  const r = state.result;
  const status = String(plan.status || '').toLowerCase();
  const submitted = /submitted|pending|approv|待审批/.test(status) && !/approved/.test(status);
  const approved = /approved|已批准/.test(status);

  const summary = '<div class="kv">' +
      '<div><span class="k">计划</span><span class="v">' + esc(plan.name || plan.id) + '</span></div>' +
      '<div><span class="k">类型</span><span class="v">' + esc(typeLabel(plan.type)) + '</span></div>' +
      '<div><span class="k">工厂</span><span class="v">' + esc(plan.factory || '—') + '</span></div>' +
      '<div><span class="k">状态</span><span class="v">' + tagHtml(statusLabel(plan.status), statusTone(plan.status)) + '</span></div>' +
      '<div><span class="k">选定方案</span><span class="v">' + esc(state.selectedCandidate || plan.selected_candidate || '未选择') + '</span></div>' +
      '<div><span class="k">计算结果</span><span class="v">' + (state.rawResult ? (state.result ? '已归一化' : '有原始结果 · 结构待接入') : '无') + '</span></div>' +
    '</div>';

  let actions;
  if (approved) {
    actions = '<div class="note ok">该计划已被主管批准。演示环境不会向 ERP / MES 下发任何内容。</div>';
  } else if (submitted) {
    actions = '<div class="note warn">计划已提交，等待生产主管在“待办审批”处理；审批中内容已冻结。</div>' +
      (canApprovePlan() ? '<div class="row" style="margin-top:10px"><button type="button" class="btn primary" data-act="approve" data-id="' + escAttr(String(plan.id)) + '">作为主管批准</button></div>' : '');
  } else {
    const cand = (state.result && state.result.candidates || []).filter(function (c) { return c.key === state.selectedCandidate; })[0] || null;
    const ready = !!state.selectedCandidate && !!state.rawResult;
    const submittable = cand ? cand.submittable : false;
    const blockers = cand ? (cand.submitBlockers || []) : [];
    actions =
      '<div class="row">' +
        '<button type="button" class="btn primary" data-act="submitPlan" data-id="' + escAttr(String(plan.id)) + '"' +
          (canSubmitPlan() && ready && submittable && !statusLocked() ? '' : ' disabled') + '>确认提交方案</button>' +
        '<a class="btn" href="#/plan/' + escAttr(String(plan.id)) + '/5">返回方案分析</a>' +
      '</div>' +
      (!ready ? '<div class="hint" style="margin-top:6px">需要先有计算结果并选定方案才能提交。</div>' : '') +
      (ready && !submittable
        ? '<div class="note bad" style="margin-top:8px">引擎判定该方案不可提交，阻断项：' +
          (blockers.length ? '<ul style="margin:4px 0 0;padding-left:18px">' + blockers.map(function (b) { return '<li>' + esc(b) + '</li>'; }).join('') + '</ul>' : '服务端未给出明细') +
          '</div>'
        : '') +
      (!canSubmitPlan() ? '<div class="hint" style="margin-top:6px">当前角色不执行提交；提交由计划员发起，批准由主管执行。</div>' : '');
  }

  return '<div class="card"><div class="card-head"><h2 class="card-title">第 6 步 · 确认提交</h2>' +
      '<span class="card-sub">提交前请确认方案与工厂范围</span></div>' +
    '<div class="card-body">' +
      '<div class="note bad"><b>演示环境，未接入生产。</b>本页的提交与批准仅写入本机后端演示状态，' +
        '不会向 ERP / MES 下发计划、不会生成生产指令，也不代表任何真实生效范围。</div>' +
      '<div class="note bad" style="margin-top:6px"><b>研究模型警示：</b>计算内核未实现人工冻结、冻结区与已投产保护，' +
        '也未实现完整原文工艺规则，因此不能用于实际生产决策，也不会保护真实已投产车辆。' +
        '提交前请确认输入中<b>不含真实已投产或冻结订单</b>。</div>' +
      '<div style="margin-top:12px">' + summary + '</div>' +
      '<div style="margin-top:12px">' + actions + '</div>' +
    '</div></div>' +
    '<div class="card"><div class="card-head"><h2 class="card-title">提交前检查</h2></div><div class="card-body tight">' +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>检查项</th><th>当前情况</th></tr></thead><tbody>' +
        checkRow('计划类型是否已实现计算', isComputable(plan.type) ? tagHtml('已实现', 'ok') : tagHtml('未实现', 'warn')) +
        checkRow('是否已有计算结果', state.rawResult ? tagHtml('有', 'ok') : tagHtml('无', 'warn')) +
        checkRow('结果是否已归一化', state.result ? tagHtml('是', 'ok') : (state.rawResult ? tagHtml('否 · 结构待接入', 'warn') : '—')) +
        checkRow('是否已选定方案', state.selectedCandidate ? tagHtml(esc(state.selectedCandidate), 'ok') : tagHtml('未选择', 'warn')) +
        checkRow('引擎是否判定可提交', submitGateHtml()) +
        checkRow('统一校验结果', validationSummary()) +
        checkRow('冻结 / 已投产保护', tagHtml('未实现', 'bad')) +
        checkRow('生产下发', tagHtml('不执行（演示环境）', 'bad')) +
      '</tbody></table></div></div></div>';
}

function checkRow(k, v) {
  return '<tr><td>' + esc(k) + '</td><td>' + v + '</td></tr>';
}

function submitGateHtml() {
  if (!state.result) return '—';
  const cand = (state.result.candidates || []).filter(function (c) { return c.key === state.selectedCandidate; })[0];
  if (!cand) return tagHtml('未选择方案', 'warn');
  if (cand.submittable) return tagHtml('可提交', 'ok');
  return tagHtml('不可提交（' + (cand.submitBlockers || []).length + ' 项阻断）', 'bad');
}

function validationSummary() {
  if (!state.result) return '—';
  const opt = state.result.candidates.filter(function (c) { return c.key === 'optimized'; })[0] || state.result.candidates[0];
  const s = opt.validationSummary;
  if (!s || !s.total) return tagHtml('结果未提供', 'warn');
  return s.passed ? tagHtml(s.total + ' 项全部通过', 'ok') : tagHtml(s.failed + ' / ' + s.total + ' 项未通过', 'bad');
}

/* ============================ 11. 交互动作 ============================ */

async function runAction(name, el) {
  const id = el.dataset.id;
  try {
    switch (name) {
      case 'bannerRetry': {
        const what = el.dataset.retry;
        if (what === 'me') { await loadMe(); if (state.route.view === 'plans') await loadPlans(); }
        if (what === 'plans') await loadPlans();
        if (what === 'plan') await loadWorkspace(state.route.id);
        render();
        return;
      }
      case 'reloadPlans': await loadPlans(); return;
      case 'reloadMe': await loadMe(); await loadSystem(); render(); return;
      case 'reloadSystem': await loadSystem(); render(); return;
      case 'reloadSample': await loadSample(); return;
      case 'reloadPlan': await loadWorkspace(id); return;
      case 'reloadDetail': await loadPlanDetail(id, true); return;

      case 'newPlan':
        resetWorkspace();
        state.newPlan = { name: defaultPlanName('W+3'), type: 'W+3', factory: firstFactory() };
        go('#/plan/new/1');
        render();
        return;

      case 'toggleDetail': {
        const key = String(id);
        state.expanded = state.expanded === key ? null : key;
        render();
        if (state.expanded === key && !state.details[key]) await loadPlanDetail(key, true);
        return;
      }

      case 'openPlan':
        go('#/plan/' + id + '/' + (el.dataset.step || '1'));
        return;

      case 'openTask': {
        // 先记录待打开的任务，再由路由加载完成后执行，避免与 loadWorkspace 竞态
        state.pendingTaskId = String(el.dataset.task);
        if (state.route.view === 'workspace' && String(state.route.id) === String(id)) {
          const t = state.pendingTaskId;
          state.pendingTaskId = null;
          await openTask(t);
        } else {
          go('#/plan/' + id + '/4');
        }
        return;
      }

      case 'gotoStep':
        go('#/plan/' + (state.plan ? state.plan.id : 'new') + '/' + el.dataset.step);
        return;

      case 'createPlan': {
        if (state.actionBusy) return;
        const name = String(state.newPlan.name || '').trim() || defaultPlanName(state.newPlan.type);
        const payload = { name: name, type: state.newPlan.type, factory: state.newPlan.factory || firstFactory() };
        state.actionBusy = 'create';
        try {
          const d = await api.createPlan(payload);
          const plan = d && d.plan ? d.plan : d;
          if (!plan || plan.id === undefined) throw new ApiError('创建接口未返回 plan.id', 0);
          state.plan = plan;
          state.planId = String(plan.id);
          hydrateEditorFromPlan();
          state.backendOk = true;
          toast('计划已创建并保存到后端：' + (plan.name || plan.id), 'ok');
          go('#/plan/' + plan.id + '/2');
        } finally {
          state.actionBusy = null;
        }
        await loadPlans();
        return;
      }

      case 'saveName': {
        const name = String(state.planDraftName || '').trim();
        if (!name) { toast('计划名称不能为空', 'bad'); return; }
        const d = await api.patchPlan(id, { name: name });
        const plan = d && d.plan ? d.plan : d;
        if (plan && plan.id !== undefined) state.plan = plan; else state.plan.name = name;
        state.planDraftName = '';
        toast('名称已保存', 'ok');
        await loadPlans();
        return;
      }

      case 'loadSample': {
        const d = await api.sample();
        state.sample = d;
        state.editor.inputText = safeJson(d && d.sample ? d.sample : d);
        state.editor.dirty = true;
        updateJsonStateEls();
        render();
        toast('已载入服务端样例，确认后点击“保存输入”', 'ok');
        return;
      }

      case 'reloadInput':
        await refreshPlanQuiet(id);
        hydrateEditorFromPlan();
        toast('已重载服务端已保存的输入');
        render();
        return;

      case 'formatInput': {
        const parsed = tryParseJson(state.editor.inputText);
        if (parsed.error) { toast('JSON 格式错误，无法格式化：' + parsed.error, 'bad'); return; }
        state.editor.inputText = safeJson(parsed.value);
        render();
        return;
      }

      case 'saveInput': {
        const parsed = tryParseJson(state.editor.inputText);
        if (parsed.error) { state.editor.inputError = parsed.error; toast('JSON 格式错误：' + parsed.error, 'bad'); updateJsonStateEls(); return; }
        const d = await api.patchPlan(id, { input: parsed.value });
        const plan = d && d.plan ? d.plan : d;
        if (plan && plan.id !== undefined) state.plan = plan; else state.plan.input = parsed.value;
        hydrateEditorFromPlan();
        toast('输入已保存到后端', 'ok');
        await loadPlans();
        render();
        return;
      }

      /* ---- 新增：XML 场景载入与预览 ---- */
      case 'loadPresetScenarios': {
        await loadPresetScenarios();
        render();
        return;
      }
      case 'loadXmlScenario': {
        if (state.actionBusy) return;
        state.actionBusy = 'loadXml';
        try {
          const scId = el.dataset.id;
          /* 场景卡片的 data-id 是场景 ID；计划 ID 必须取当前工作空间上下文，
             否则会把场景 ID 当计划 ID 发给后端（404）。 */
          const planId = (state.plan && state.plan.id) || state.route.id || id;
          const d = await api.loadXmlScenario(planId, scId);
          const plan = d && d.plan ? d.plan : d;
          if (plan && plan.id !== undefined) state.plan = plan;
          hydrateEditorFromPlan();
          state.xmlPreviewOpen = false;
          toast('XML 接口实例已载入：' + scId, 'ok');
          render();
        } finally {
          state.actionBusy = null;
        }
        return;
      }
      case 'toggleXmlPreview': {
        const box = document.getElementById('xmlPreviewContainer');
        if (box) box.style.display = box.style.display === 'none' ? 'block' : 'none';
        return;
      }
        case 'showDbRecords': {
          const d = await api.getDbRecords(id);
          if (d && d.plan_id) {
            state.dbRecords = d;
            state.dbRecordsOpen = true;
            renderDbRecordsModal();
          }
          return;
        }
        case 'closeDbRecords': {
          const modal = document.getElementById('modal');
          if (modal) modal.hidden = true;
          state.dbRecordsOpen = false;
          return;
        }

      case 'saveSchema': {
        const problems = applySchemaEdits();
        if (problems.length) { toast(problems.join('；'), 'bad'); return; }
        const d = await api.patchPlan(id, { input: state.schema });
        const plan = d && d.plan ? d.plan : d;
        if (plan && plan.id !== undefined) state.plan = plan; else state.plan.input = state.schema;
        hydrateEditorFromPlan();
        toast('约束与参数已保存（计算结果已清除，任务历史保留）', 'ok');
        render();
        return;
      }

      case 'reloadSchema':
        await refreshPlanQuiet(id);
        hydrateEditorFromPlan();
        toast('已放弃修改并重载服务端输入');
        render();
        return;

      case 'runCompute': {
        if (state.actionBusy) return;
        state.actionBusy = 'run';
        try {
          const d = await api.run(id);
          const taskId = d && (d.task_id || d.taskId || (d.task && d.task.id));
          if (!taskId) throw new ApiError('计算接口未返回 task_id', 0);
          state.taskId = String(taskId);
          state.taskDetail = { id: taskId, status: 'queued', stage: '服务端排队中' };
          try { localStorage.setItem('aps.lastTask.' + id, String(taskId)); } catch (e) { /* 忽略 */ }
          toast('计算任务已提交：' + taskId);
          await refreshPlanQuiet(id);
          startPoll(taskId, true);
          render();
        } finally {
          state.actionBusy = null;
        }
        return;
      }

      case 'refreshTask':
        if (!state.taskId) { toast('当前没有任务', 'bad'); return; }
        applyTask(state.taskId, await api.task(state.taskId));
        if (taskIsRunning(state.taskDetail)) startPoll(state.taskId, true);
        render();
        return;

      case 'stopPoll':
        stopPoll();
        toast('已停止自动刷新；任务仍在服务端运行');
        render();
        return;

      case 'pickCandidate':
        state.selectedCandidate = el.value;
        render();
        return;

      case 'submitPlan':
        requestSubmitConfirm(id);
        return;

      case 'approve':
        requestApproveConfirm(id);
        return;

      case 'confirmCancel':
        state.confirm = null;
        renderConfirm();
        return;

      case 'confirmOk': {
        const c = state.confirm;
        if (!c) return;
        state.confirm = null;
        renderConfirm();
        if (c.kind === 'submit') {
          const d = await api.submit(c.id, state.selectedCandidate);
          const plan = d && d.plan ? d.plan : d;
          if (plan && plan.id !== undefined) state.plan = plan; else state.plan.status = 'pending';
          toast('已提交，等待主管审批（演示环境，未下发生产）', 'ok');
          await loadPlans();
          render();
        } else if (c.kind === 'approve') {
          const d = await api.approve(c.id);
          const plan = d && d.plan ? d.plan : d;
          if (plan && plan.id !== undefined) { state.plan = plan; state.details = {}; }
          toast('已批准（演示环境，未下发生产）', 'ok');
          await loadPlans();
          render();
        }
        return;
      }

      case 'askPreset':
        askQuestion(el.dataset.q);
        return;

      case 'askAssistant': {
        const input = document.getElementById('assistantQ');
        askQuestion(input ? input.value : '');
        return;
      }

      case 'saveApiBase': {
        const input = document.getElementById('apiBaseInput');
        const v = input ? String(input.value || '').trim().replace(/\/+$/, '') : '';
        try { localStorage.setItem('aps.apiBase', v); } catch (e) { /* 忽略 */ }
        state.backendOk = null;
        toast('已保存 API 基地址并重连');
        await loadMe();
        await loadPlans();
        await loadSample();
        render();
        return;
      }

      default:
        return;
    }
  } catch (e) {
    handleActionError(e, name);
  }
}

function handleActionError(e, name) {
  const msg = (e && e.message) ? e.message : '操作失败';
  if (e && e.status === 403) {
    toast('权限不足：' + msg, 'bad');
  } else if (e && e.status === 422) {
    toast('服务端拒绝执行（422）：' + msg + ' —— 该计划类型的计算未实现，本页不会用 W+3 算法代替。', 'bad');
  } else if (e && e.status === 0) {
    state.backendOk = false;
    toast('后端不可达：' + msg, 'bad');
  } else {
    toast('操作失败（' + name + '）：' + msg, 'bad');
  }
  render();
}

/** 把步骤 3 的结构化控件写回引擎输入，并清理仅用于界面的伪字段。 */
function applySchemaEdits() {
  const s = state.schema;
  if (!s || typeof s !== 'object') return ['当前没有可保存的输入快照'];

  if (s.__workdays !== undefined) {
    const raw = String(s.__workdays === null ? '' : s.__workdays).trim();
    const parts = raw === '' ? [] : raw.split(',').map(function (x) { return x.trim(); }).filter(function (x) { return x !== ''; });
    const nums = parts.map(Number);
    if (!nums.length || nums.some(function (n) { return !isFinite(n) || Math.floor(n) !== n; })) {
      return ['工作日必须是逗号分隔的整数 day 序号'];
    }
    if (!s.calendar || typeof s.calendar !== 'object') s.calendar = {};
    s.calendar.workdays = nums;
  }

  const problems = [];
  Object.keys(state.processModelText || {}).forEach(function (stage) {
    if (!s.process_times || typeof s.process_times[stage] !== 'object') return;
    const txt = String(state.processModelText[stage] || '').trim();
    if (txt === '') { delete s.process_times[stage].by_model; return; }
    let v;
    try { v = JSON.parse(txt); }
    catch (e) { problems.push((STAGE_FALLBACK[stage] || stage) + ' 车型覆盖 JSON 错误：' + e.message); return; }
    if (!v || typeof v !== 'object' || Array.isArray(v)) {
      problems.push((STAGE_FALLBACK[stage] || stage) + ' 车型覆盖必须是 JSON 对象'); return;
    }
    const bad = Object.keys(v).filter(function (k) { return v[k] === null || isNaN(Number(v[k])); });
    if (bad.length) { problems.push((STAGE_FALLBACK[stage] || stage) + ' 车型 ' + bad.join('、') + ' 的值不是数字'); return; }
    s.process_times[stage].by_model = v;
  });
  if (problems.length) return problems;

  if (s.calendar && s.calendar.max_cars_per_day === null) delete s.calendar.max_cars_per_day;
  if (s.time_limit_seconds !== undefined && s.time_limit_seconds !== null && !(Number(s.time_limit_seconds) > 0)) {
    return ['求解时限必须大于 0'];
  }
  if (s.drain_days !== undefined && s.drain_days !== null && Number(s.drain_days) < 0) {
    return ['收尾天数不能小于 0'];
  }

  Object.keys(s).forEach(function (k) { if (k.indexOf('__') === 0) delete s[k]; });
  return [];
}

function tryParseJson(text) {
  const t = String(text || '').trim();
  if (!t) return { error: '内容为空，请先载入或上传 JSON' };
  try { return { value: JSON.parse(t) }; }
  catch (e) { return { error: String(e.message).replace(/^JSON\.parse:\s*/, '').slice(0, 120) }; }
}

async function refreshPlanQuiet(id) {
  try {
    const d = await api.plan(id);
    const plan = d && d.plan ? d.plan : d;
    if (plan && plan.id !== undefined) {
      state.plan = plan;
      state.details[String(id)] = { loading: false, error: null, plan: plan };
    }
  } catch (e) { /* 静默 */ }
}

async function openTask(taskId) {
  stopPoll();
  state.taskId = String(taskId);
  try { localStorage.setItem('aps.lastTask.' + state.route.id, String(taskId)); } catch (e) { /* 忽略 */ }
  try {
    const d = await api.task(taskId);
    applyTask(taskId, d);
    if (taskIsRunning(d)) startPoll(taskId, true);
    else if (!d || !d.result) toast('该任务没有返回结果：' + statusLabel(d && d.status), 'bad');
  } catch (e) {
    state.taskError = e.message || '任务查询失败';
    toast('任务查询失败：' + state.taskError, 'bad');
  }
  render();
}

function askQuestion(q) {
  const question = String(q || '').trim();
  if (!question) { toast('请输入问题', 'bad'); return; }
  const answer = assistantAnswer(question);
  state.assistantLog.unshift({ q: question, a: answer });
  state.assistantLog = state.assistantLog.slice(0, 6);
  render();
}

function defaultPlanName(type) {
  return type + ' 计划 ' + nowStamp();
}

function firstFactory() {
  const fs = state.me && Array.isArray(state.me.factories) ? state.me.factories : [];
  return fs.length ? fs[0] : 'F1';
}

function updateJsonStateEls() {
  const j = document.getElementById('jsonState');
  if (j) j.innerHTML = jsonStateHtml(state.editor.inputText);
}

/* ============================ 12. 事件绑定 ============================ */

document.addEventListener('click', function (e) {
  const kind = e.target.closest('[data-act="pickCandidate"]');
  if (kind) { state.selectedCandidate = kind.value; return; }

  const el = e.target.closest('[data-act]');
  if (!el || el.disabled) return;

  const name = el.dataset.act;
  // 文件选择与单选由原生控件或 change 事件处理，避免重复触发
  if (name === 'uploadMock' || name === 'pickCandidate') return;
  if (el.tagName === 'INPUT' && el.type === 'radio') return;

  runAction(name, el);
});

document.addEventListener('change', function (e) {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  if (el.dataset.act === 'uploadMock') {
    const f = el.files && el.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = function () {
      const text = String(reader.result || '');
      const parsed = tryParseJson(text);
      if (parsed.error) { toast('上传文件不是合法 JSON：' + parsed.error, 'bad'); return; }
      state.editor.inputText = safeJson(parsed.value);
      state.editor.dirty = true;
      toast('已载入上传文件，确认后点击“保存输入”', 'ok');
      render();
    };
    reader.onerror = function () { toast('文件读取失败', 'bad'); };
    reader.readAsText(f);
    return;
  }
  if (el.dataset.act === 'pickCandidate') { state.selectedCandidate = el.value; return; }
});

document.addEventListener('input', function (e) {
  const el = e.target.closest('[data-bind]');
  if (!el) return;
  const bind = el.dataset.bind;

  // 步骤 3：写入引擎真实字段。数字控件按数字存，空值删除键（如日车辆上限留空）
  if (bind.indexOf('schema.') === 0 || bind.indexOf('processModelText.') === 0) {
    if (bind.indexOf('processModelText.') === 0) {
      state.processModelText[bind.slice('processModelText.'.length)] = el.value;
      return;
    }
    const path = bind.slice('schema.'.length);
    if (el.dataset.cast === 'number') {
      const raw = String(el.value).trim();
      if (raw === '') { setPath(state.schema, path, null); }
      else {
        const n = Number(raw);
        setPath(state.schema, path, isNaN(n) ? raw : n);
      }
    } else {
      setPath(state.schema, path, el.value);
    }
    return;
  }

  setPath(state, bind, el.value);

  if (bind === 'editor.inputText') {
    state.editor.dirty = true;
    const j = document.getElementById('jsonState');
    if (j) j.innerHTML = jsonStateHtml(el.value);
    return;
  }
  if (bind === 'search' || bind === 'filterType') {
    const tbody = document.querySelector('#planTableBody');
    if (tbody) {
      tbody.innerHTML = planRowsHtml(filteredPlans());
    } else {
      render();
    }
    return;
  }
  if (bind === 'newPlan.type') {
    if (!state.newPlan.name || /^\S+ 计划 \d{4}-\d{2}-\d{2}/.test(state.newPlan.name)) {
      state.newPlan.name = defaultPlanName(state.newPlan.type);
    }
    render();
    return;
  }
});

document.addEventListener('change', function (e) {
  const el = e.target.closest('[data-bind]');
  if (!el) return;
  if (el.dataset.bind === 'filterType' || el.dataset.bind === 'newPlan.type' || el.dataset.bind === 'newPlan.factory') {
    setPath(state, el.dataset.bind, el.value);
    const tbody = document.querySelector('#planTableBody');
    if (tbody && el.dataset.bind === 'filterType') { tbody.innerHTML = planRowsHtml(filteredPlans()); return; }
    render();
  }
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && state.confirm) { state.confirm = null; renderConfirm(); return; }
  if (e.key !== 'Enter') return;
  const el = e.target;
  if (el && el.id === 'assistantQ') { e.preventDefault(); askQuestion(el.value); }
  if (el && el.id === 'planSearch') { e.preventDefault(); }
});

window.addEventListener('hashchange', function () { handleRoute(); });

document.getElementById('reloadBtn').addEventListener('click', async function () {
  stopPoll();
  await loadMe();
  if (state.route.view === 'workspace') await loadWorkspace(state.route.id);
  else await loadPlans();
  render();
});

document.getElementById('userSelect').addEventListener('change', async function (e) {
  stopPoll();
  setCurrentUser(e.target.value);
  // 新身份必须清空整个页面上下文，防止旧身份的在途响应回填缓存。
  const target = e.target.value === 'admin' ? 'system' : 'plans';
  window.location.replace(window.location.pathname + '?identity=' + encodeURIComponent(e.target.value) + '#/' + target);
});

async function handleRoute() {
  const next = parseHash();
  const prev = state.route;

  // 服务端菜单决定可见视图：非授权视图回落到该身份的第一个菜单
  if (!viewAllowed(next.view) && state.me) {
    const fallback = firstAllowedMenu();
    if ('#/' + fallback !== location.hash) {
      location.hash = '#/' + fallback;
      return;
    }
  }

  state.route = next;

  if (next.view === 'workspace') {
    await loadWorkspace(next.id);
    if (state.pendingTaskId) {
      const t = state.pendingTaskId;
      state.pendingTaskId = null;
      await openTask(t);
    }
    render();
  } else {
    stopPoll();
    if (prev.view === 'workspace') resetWorkspace();
    render();
    if (next.view === 'plans' || next.view === 'inbox') {
      if (!state.plans.length || state.plansError) await loadPlans();
      else render();
    }
    if (next.view === 'rules') {
      if (!state.sample && !state.sampleError) await loadSample();
      else render();
    }
    if (next.view === 'system') {
      await loadSystem();
      render();
    }
  }
  const view = document.getElementById('view');
  if (view) view.scrollTop = 0;
  window.scrollTo(0, 0);
}

/* ============================ 13. 启动 ============================ */

(async function boot() {
  state.route = parseHash();
  render();
  await loadMe();
  render();
  if (!viewAllowed(state.route.view)) {
    const fallback = firstAllowedMenu();
    location.hash = '#/' + fallback;
    return; // hashchange 会接管后续加载
  }
  await loadPlans();
  if (state.route.view === 'workspace') await loadWorkspace(state.route.id);
  if (state.route.view === 'rules') await loadSample();
  if (state.route.view === 'system') await loadSystem();
  // 加载典型场景接口库
  loadPresetScenarios();
  render();
})();
