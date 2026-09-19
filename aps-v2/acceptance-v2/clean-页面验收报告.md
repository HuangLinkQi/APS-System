# APS 页面验收报告（V2 干净复测版，T01-T36）

环境
- 服务：`aps-v2/server_integrated.py`，端口 18745（独立验收库 `aps-v2/data/acceptance-v2.sqlite3`）。
- 入口：`http://127.0.0.1:18745`，浏览器：ego-lite。
- TaskSpace：7（agent 持有，p1 单 tab）；本次报告完成时调用 `task.finish({keep:[]})` 关闭 space7（数据库与服务保持运行，验收数据全部保留）。
- 证据目录：`/Users/huangrq25/Desktop/其他/Anio/aps-v2/acceptance-v2/clean/`
- 旧报告位置：`/Users/huangrq25/Desktop/其他/Anio/aps-v2/acceptance-v2/页面验收报告.md`（违规项保留不再修改）
- 报告生成时间：2026-09-19
- 行为约束（严格遵守）：
  - 仅使用 ego-browser 真实动作：`goto / click / fill / selectOption / press / keyboard / reload / snapshot / screenshot / waitForTimeout / mouse.wheel`。
  - 禁止使用：`page.evaluate`、`fetch`、`xhr`、`document.*`、`window.*`、`page.cdp`、`playwright` / `puppeteer` 等任何 DOM 读取或内部 API 调用。
  - 所有"页面断言"以 snapshot 文本（.txt）或截图（.png）肉眼可见文字为准；不做 JSON 字段引用、不读 API。
  - 新计划统一以 `CLEAN-T{XX}-{描述}` 命名通过页面创建；现有库保留未清。
  - 浏览器调用 `finish()` 关闭 space7（不留任何 Page），服务进程继续运行。

---

## 1. 摘要

| 项 | 数量 |
|---|---|
| 用例总数 | 36（T01-T36） |
| 普通 PASS | 34 |
| 边界 PASS | 2（T26 事件展示非动态生效；T27 引擎限制 ORDER_CANCEL） |
| FAIL / 缺陷 | 0 |
| 总计 | **36 = 34 普通 PASS + 2 边界 PASS** |
| 准出 | 是（提交主会话审核） |
| 修复项复测 | 4/4 PASS |

T01 复用主会话首测 `aps-v2/acceptance-v2/01-empty.{png,txt}`（真实 0 条空库）作为空态基准，不重复创建避免与黑盒纪律冲突。

---

## 2. 违规项历史（保留旧记录）

旧 `页面验收报告.md` 曾使用 `page.evaluate(() => fetch('/api/...'))` 读取 `plan.input.source_name / metrics / tasks` 等 JSON 字段并写入"页面断言"段落。该报告不再修改以保留违规时间线。本 CLEAN 报告所有断言严格基于 DOM 可见文字、截图与 snapshot 文本。

---

## 3. 测试计划与 URL（依据保存的真实页面快照）

主会话首测计划（保留，未清空）：
- `http://127.0.0.1:18745/#/plan/1a09a138d80a` — 验收V2-正常生产-01（draft）

主会话早期计划（保留）：
- `http://127.0.0.1:18745/#/plan/5b74a5970104` — T03-验收新草稿（approved）

CLEAN 干净复测创建的计划：
- `http://127.0.0.1:18745/#/plan/5049675216bb` — CLEAN-T05-正常场景（approved）
- `http://127.0.0.1:18745/#/plan/f779b96d8330` — CLEAN-T19-节拍对照（ready）
- `http://127.0.0.1:18745/#/plan/3bb75ac2a9d1` — CLEAN-T24-缺料（ready）
- `http://127.0.0.1:18745/#/plan/1975e09e16a1` — CLEAN-T25-延期（ready）
- `http://127.0.0.1:18745/#/plan/91334a17199d` — CLEAN-T26-紧急（ready）
- `http://127.0.0.1:18745/#/plan/7dc91ae23290` — CLEAN-T27-撤单（ready）
- `http://127.0.0.1:18745/#/plan/9db9307296f6` — CLEAN-T28-瓶颈（ready）
- `http://127.0.0.1:18745/#/plan/169f1602e860` — CLEAN-T36-已排未完成（ready）

CLEAN 补验创建的计划：
- `http://127.0.0.1:18745/#/plan/a9b10a579b37` — CLEAN-T34b-切换带历史（draft，scenario=case_02）

---

## 4. 逐项验收（36 项）

### T01 — 空库首次打开计划管理
- 身份：planner-a；URL `http://127.0.0.1:18745/#/plans`。
- 证据来源：主会话首测真实空库截图（验收库初始 0 计划）。
- 文件：`/Users/huangrq25/Desktop/其他/Anio/aps-v2/acceptance-v2/01-empty.png`、`01-empty.txt`。
- 复测状态：库已含 1 条主会话首测草稿（保留未清），不再重测避免与黑盒纪律冲突。
- 结果：**PASS**

### T02 — 空名称提交 + 取消
- URL 起点：`http://127.0.0.1:18745/#/plans`
- 动作序列：点"新建排程"按钮 → 模态打开 → 不填名称直接点"下一步" → 浏览器原生校验 "请填写此字段"（红框提示），模态保持 → 填名"T02-CLEAN-cancel" → 点"取消" → 模态关闭
- 证据：`CLEAN-T02-step1-plans.{png,txt}` … `CLEAN-T02-step5-after-cancel.{png,txt}`。关键截图：`CLEAN-T02-step3-empty-rejected.png` 显示模态中 input 红框 + "请填写此字段" tooltip。
- 结果：**PASS**

### T03 — 输入名称 + 工厂 + 下一步
- 动作：填名"CLEAN-T03-新草稿"、工厂默认 F1、点"下一步"。
- 页面观察：URL `#/plan/{new}`；弹窗标题"当前为演示数据，请选择你要测试的场景"；副文"选择后自动准备全部输入，不需要后续重复导入。更换场景将清除本计划当前计算结果，历史任务保留。"；7 个场景按钮。
- 证据：`CLEAN-T03-step1-scenario-modal.{png,txt}`。
- 结果：**PASS**

### T04 — 取消场景 + 刷新 + 重开
- 动作：点"取消选择" → 浏览器刷新（reload）→ 点"下一步：准备数据" → 弹窗再次出现。
- 页面观察：reload 后 plan 仍存在；1·准备数据 卡片显示"尚未选择演示场景。选择后将一次准备订单、供应、产线、节拍和日历。" + 按钮"下一步：准备数据"。
- 证据：`CLEAN-T04-step1..3-{after-cancel,after-reload,reopen-modal}.{png,txt}`。
- 结果：**PASS**

### T05 — 加载场景自动准备
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：新建"CLEAN-T05-正常场景" → 点"正常生产 · 12辆基准"。
- 页面观察：1·卡片显示"正常生产 · 12辆基准" + "基础资料版本 N" + "本次输入已准备完成，无需重复填写产能、节拍或工艺规则。"；按钮组"查看本次输入 / 前往基础数据 / 引用最新基础数据 / 更换演示场景"。
- 证据：`CLEAN-T05-step1..2-{modal,loaded}.{png,txt}`。
- 结果：**PASS**

### T06 — 展开输入详情
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb/input`
- 动作：点"查看本次输入" → 依次点击各 summary（订单/初始库存/到货计划/来源事件/人员资源/产线资源/生产节拍/生产日历/工序衔接/切换规则）。
- 页面观察：订单 12 辆（VIN_01…VIN_12 含 VIN_04_PULLOUT）；初始库存 7 项；到货计划 1 项；来源事件 0 条；人员资源 5 项；产线资源 5 行；生产节拍 11 行；生产日历 3 行；工序衔接 2 行；切换规则（演示假设）展开多行。
- 证据：`CLEAN-T06-step1..4-{input-page,...,all-expanded}.{png,txt}`。
- 结果：**PASS**

### T07 — planner 只读
- URL：`http://127.0.0.1:18745/#/rules/F1%3Acase_00_micro_benchmark_12`
- 动作：planner-a → 上述 URL。
- 页面观察：scope "计划员 A · F1"；版本卡"版本 N · F1"；note "当前为只读查看。需要修改时请由工艺负责人维护，再回到计划引用最新版本。"；表格内仅文本（无 input/select 可编辑）。
- 证据：`CLEAN-T07-step1-planner-master.{png,txt}`。
- 结果：**PASS**

### T08 — engineer 可编辑
- URL：同上。
- 动作：engineer → 同一 URL。
- 页面观察：scope "工艺负责人 · F1"；版本卡 + "保存新版本 / 放弃修改" 按钮（在底部 sticky 区）；表格内 input/select 可编辑。
- 证据：`CLEAN-T08-step1..2-{engineer-master,engineer-scrolled}.{png,txt}`。
- 结果：**PASS**

### T09 — 节拍 0 / 负值
- URL：`http://127.0.0.1:18745/#/rules/F1%3Acase_00_micro_benchmark_12`
- 动作：SUV_WHITE 焊装节拍 = 0 → 保存；= -5 → 保存。
- 页面观察：两次失败，反馈 toast/页面文字"SUV_WHITE 焊装节拍（分钟）须为不小于1的整数"。
- 证据：`CLEAN-T09-step1..3-{before,zero-rejected,negative-rejected}.{png,txt}`。
- 结果：**PASS**

### T10 — 清空节拍阻断计算
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：清空 SUV_WHITE 焊装节拍 → 保存 → planner-a 打开 CLEAN-T05-正常场景 → 点"引用最新基础数据" → 确认。
- 页面观察：plan 1·卡片出现红框 note"数据待完善：SUV_WHITE缺少焊装有效节拍"；2·卡片"生成方案"按钮明显 disabled 样式（淡色）。
- 证据：`CLEAN-T10-step1..6-{empty-saved,...,after-refresh}.{png,txt}`。
- 结果：**PASS（按用户最新口径：允许存不完整但阻断计算）**

### T11 — 修复节拍恢复计算
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：engineer 填回 15 → 保存 → planner 引用最新。
- 页面观察：plan 1·卡片回到绿色 note"本次输入已准备完成，无需重复填写产能、节拍或工艺规则。"；2·卡片"生成方案"按钮恢复 primary 样式。
- 证据：`CLEAN-T11-step1..2-{master-repaired,plan-recovered}.{png,txt}`。
- 结果：**PASS**

### T12 — 产线资源增/停用/启用/持久化（清洁复测）
- URL：`http://127.0.0.1:18745/#/rules/F1%3Acase_00_micro_benchmark_12`
- 动作（清洁复测）：
  1. 点"新增产线" → 新增空行 → 点"保存新版本" → toast "产线编号必填且不能重复"（拒绝空编号）
  2. 点"放弃修改" → 资源表回到基线
  3. W_LINE select enabled → false → 保存 → 红色警告"焊装需恰好1条启用产线；当前引擎不支持同工序多产线调度"，W_LINE 状态"停用"
  4. W_LINE 恢复"启用" → "资料完整，可供排程引用。"
  5. 备用线 W_LINE_BACKUP：select enabled → true → 保存 → rev 升 62；随后恢复 → false → rev 升 63，"资料完整"
  6. 浏览器刷新 → 版本卡仍显示 rev 63 "资料完整，可供排程引用。"
- 资源表最终状态：5 行（W_LINE 启用/P_LINE 启用/A_LINE 启用/W_LINE_BACKUP 停用/Q_LINE 停用）
- 证据：`CLEAN-T12b-step1-empty-id-rejected.{png,txt}`、`CLEAN-T12b-step7-resources-table.{png,txt}`、`CLEAN-T12b-step9-backup-enabled-result.{png,txt}`、`CLEAN-T12b-step12-after-enable-backup-table.{png,txt}`、`CLEAN-T12b-step13-reload-version.{png,txt}`。
- 结果：**PASS**

### T13 — 班次开始>结束 / 同日重叠
- URL：`http://127.0.0.1:18745/#/rules/F1%3Acase_00_micro_benchmark_12`
- 动作：shifts.0.start=100, shifts.0.end=50 → 保存 → toast "班次开始必须早于结束，且不能超出当日或计算时域"；shifts.0.end=200, shifts.1.day=1, shifts.1.start=100, shifts.1.end=300 → 保存 → toast "班次时间不能重叠"。
- 证据：`CLEAN-T13-step1..4-{shifts-open,start-end,overlap,restored}.{png,txt}`。
- 结果：**PASS**

### T14 — 负转运/负缓冲/负切换分别拒绝
- URL：`http://127.0.0.1:18745/#/rules/F1%3Acase_00_micro_benchmark_12`
- 动作：buffers.0.minutes = -3 → "BUF_WP转运分钟须为不小于0的整数"；buffers.0.capacity = -1 → "BUF_WP缓冲容量须为不小于1的整数"；setups.0.minutes = -7 → "SUV_WHITE切换分钟须为不小于0的整数"；合法修改 buffers.minutes=7, setups.minutes=8 → "资料完整，可供排程引用。"
- 证据：`CLEAN-T14-step1..6-{detail-open,neg-buffer,neg-capacity,neg-setup,legal-mod,restored}.{png,txt}`。
- 结果：**PASS**

### T15 — 新版不引用到旧计划
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：planner-a 打开 → 1·卡片版本号 X；engineer 改 SUV_WHITE 焊装节拍 15→17 → 版本号升 X+1；planner 重开 → 版本号仍 X；planner 点"引用最新基础数据" → 确认 → 版本号变为 X+1（输入时间戳更新）。
- 证据：`CLEAN-T15-step1..5-{plan-before,...,restored}.{png,txt}`。
- 结果：**PASS**

### T16 — 生成方案
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：CLEAN-T05 状态 ready → 点"生成方案" → 等待计算 → 状态变为"计算完成"。
- 页面观察：进度区"正在计算，请等待真实计算结果。"→ 状态副标题"计算完成"；3·查看并选择方案出现 4 候选按钮。
- 证据：`CLEAN-T16-step1..3-{planner-view,running,completed}.{png,txt}`。
- 结果：**PASS**

### T17 — 4 候选切换
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：依次点击"交期优先/颜色集中/负荷均衡/综合优化"。
- 页面观察：每候选 h3 标题变为对应中文名；指标网格更新。
  - EDD: 未完成订单 0 台（未排0，已排未完成0）、完成准时率 100.0%、总拖期 0 分钟（加权）、换色次数 9 次、完工跨度 2961 分钟
  - ALNS: 换色次数 5 次、完工跨度 2961 分钟、首车 VIN_07
- 证据：`CLEAN-T17-step1..5-{candidates-area,EDD,COLOR,SMOOTH,ALNS}.{png,txt}`。
- 结果：**PASS**

### T18 — 正常调度对照
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：综合优化 → 展开"各工序时间" → 滚动截图。
- 页面观察：VIN_01 焊装 W_LINE 47/47/64/64、涂装 P_LINE 87/87/132/132、总装 A_LINE 146/146/171/171；VIN_04_PULLOUT 仅 焊装/涂装两行（无总装）。
- 证据：`CLEAN-T18-step1..2-{process-times,process-times-scrolled}.{png,txt}`。
- 结果：**PASS**

### T19 — 节拍 25→40 严格同 VIN 同策略对照
- 计划 URL：`http://127.0.0.1:18745/#/plan/f779b96d8330`
- 关键证据（**同 VIN_01 + 交期优先策略**）：
  - tempo 25 run：`CLEAN-T19-step14-EDD-tempo25-vin01.png` 显示 VIN_01 总装 A_LINE **60/60/85/85**（dur 25）
  - tempo 40 run：`CLEAN-T19-step12-EDD-tempo40-vin01.png` 显示 VIN_01 总装 A_LINE **60/60/100/100**（dur 40）
- 其他证据：`CLEAN-T19-step1..13-{baseline-loaded,...,tempo25-rerun}.{png,txt}`、`CLEAN-T19-step7-history-list.png` 显示计算历史 2 条任务。
- 结论：**同一 VIN_01 + 同一策略 EDD**：tempo 25 → 总装 dur 25 (60→85)；tempo 40 → 总装 dur 40 (60→100)。工艺顺序、产线分配、班次一致。
- 结果：**PASS**

### T20 — 提交取消
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：选综合优化 → 滚动到 4·提交与审批 → 点"提交所选方案" → 模态"确认提交所选方案"正文"计划：CLEAN-T05-正常场景 / 方案：综合优化" + 警示 → 点"取消"。
- 证据：`CLEAN-T20-step1..3-{alns-area,submit-modal,after-cancel}.{png,txt}`。
- 结果：**PASS**

### T21 — 确认提交 + 刷新锁定
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：再次"提交所选方案" → "确认提交" → 浏览器刷新。
- 页面观察：状态副标题"F1 · W+3演示排程 · 待审批"；1·卡片仅有"查看本次输入 / 前往基础数据"；2·卡片仅有 note"当前角色或计划状态不允许修改输入和重新计算。"。
- 证据：`CLEAN-T21-step1..2-{after-submit,after-reload}.{png,txt}`。
- 结果：**PASS**

### T22 — 主管批准
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：切换 manager → `#/inbox` → 点"打开" → 滚动找到"批准该方案" → 点击 → 模态 → "确认批准"。
- 页面观察：inbox 显示 CLEAN-T05-正常场景 状态"待审批"；批准后状态副标题"已批准"；toast"已批准，演示记录已保留。"
- 证据：`CLEAN-T22-step1..5-{inbox,pending-detail,approve-button-visible,approve-modal,approved}.{png,txt}`。
- 结果：**PASS**

### T23 — 已批准只读
- 计划 URL：`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：reload 已批准 CLEAN-T05。
- 页面观察：状态仍"已批准"；1·卡片仅有"查看本次输入 / 前往基础数据"；2·卡片仅有 note；4·卡片绿色 note"已批准 · 生管主管 · 2026-09-18T19:21:14+00:00。仅演示记录，不是生产指令。"。
- 证据：`CLEAN-T23-step1-approved-reload.{png,txt}`。
- 结果：**PASS**

### T24 — 缺料场景
- 计划 URL：`http://127.0.0.1:18745/#/plan/3bb75ac2a9d1`
- 页面观察：
  - 1·卡片"场景3：上游严重缺料断料拦截 (Material Shortage)"
  - 输入详情：订单 12 辆、初始库存 7 项、到货计划 1 项、来源事件 0 条
  - 4 候选展开"未完成订单"：标题"未完成订单 · 3（未排3台，已排未完成0台）"，3 行 VIN_10/VIN_11/VIN_12，状态"未排入"
  - 红框 note"不可提交：存在 3 台未完工订单（未排 3 台、已排未完成 0 台）：VIN_10、VIN_11、VIN_12"
  - "提交所选方案"按钮 disabled 样式
- 证据：`CLEAN-T24-step1..3-{loaded,input-expanded,completed}.{png,txt}`、`CLEAN-T24-step4-{交期优先,颜色集中,负荷均衡,综合优化}.{png,txt}`。
- 结果：**PASS**

### T25 — 延期场景
- 计划 URL：`http://127.0.0.1:18745/#/plan/1975e09e16a1`
- 页面观察：
  - 1·卡片"场景2：关键物料突发延期断料 (Supply Delay Stress)"
  - 输入详情：订单 12 辆、到货计划 BATTERY 数量 10 到货分钟 100、来源事件 MATERIAL_DELAY 发生分钟 50
  - ALNS 候选指标：未完成订单 6 台（未排2，已排未完成4）VIN_05/07/09/10/11/12、完成准时率 33.3%、总拖期 44975 分钟（加权）、换色次数 3 次、完工跨度 2995 分钟
- 证据：`CLEAN-T25-step1..4-{loaded,input-expanded,completed,ALNS}.{png,txt}`。
- 结果：**PASS**

### T26 — 紧急事件场景
- 计划 URL：`http://127.0.0.1:18745/#/plan/91334a17199d`
- 页面观察：1·卡片"场景4：紧急 VIP 加急插单冲击 (VIP Rush Tight Due)"；计算完成；4 候选全部 0 未完工，可提交。
- **边界**：事件仅展示，页面未声称动态生效——按用户指令区分。
- 证据：`CLEAN-T26-step1..3-{loaded,input-expanded,completed}.{png,txt}`。
- 结果：**边界 PASS**

### T27 — 撤单场景
- 计划 URL：`http://127.0.0.1:18745/#/plan/7dc91ae23290`
- 页面观察：1·卡片"场景5：在制保护与订单撤单响应 (Order Cancellation)"；来源事件 2 条 ORDER_CANCEL 发生分钟 100；计算完成；4 候选全部 0 未完工。
- **边界**：场景名"在制保护与订单撤单响应"暗示在制保护语义，但页面读到 ORDER_CANCEL 事件展示后 12 辆车仍按原序列完成。清单 V2 边界声明要求"不虚称在制保护已实现"——页面如实呈现事件，未虚称任何保护行为。
- 证据：`CLEAN-T27-step1..3-{loaded,input-expanded,completed}.{png,txt}`。
- 结果：**边界 PASS**

### T28 — 瓶颈场景
- 计划 URL：`http://127.0.0.1:18745/#/plan/9db9307296f6`
- 页面观察：1·卡片"场景6：小颜色交织与铝车身工艺双瓶颈 (Color & Alum Bottleneck)"；输入详情订单 14 辆（VIN_16C_D0_01..VIN_16C_D1_06）；ALNS 候选指标：未完成订单 0、完成准时率 78.6%、总拖期 9685 分钟（加权）、换色次数 10 次、完工跨度 1909 分钟。
- 证据：`CLEAN-T28-step1..4-{loaded,input-expanded,completed,ALNS}.{png,txt}`。
- 结果：**PASS**

### T29 — planner-b 工厂隔离
- 动作：切换 planner-b → `#/plans` → 直接 hash `#/plan/1a09a138d80a` → 打开新建模态。
- 页面观察：
  - 列表空（"暂无记录"），底部 note"暂无计划。通过'新建排程'开始；系统不会自动生成演示计划。"
  - 直接 hash F1 plan：主区"无法打开" + 红框"记录不存在或不在授权范围" + 链接"返回可用入口"
- 证据：`CLEAN-T29-step1..3-{planner-b-list,planner-b-f1-plan,new-modal}.{png,txt}`。
- 结果：**PASS**

### T30 — engineer / admin 权限
- 动作：engineer `#/plans` → engineer `#/plan/1a09a138d80a` → admin `#/system` → admin `#/plans`（直接 hash）。
- 页面观察：
  - engineer `#/plans`：列表仅有"刷新列表"按钮，无"新建排程"按钮
  - engineer plan：1·卡片仅有"查看本次输入 / 前往基础数据"；2·卡片仅有 note"当前角色或计划状态不允许修改输入和重新计算。"
  - admin `#/system`：导航"系统说明"，主区显示卡片"系统说明" + note"本机演示服务已连接。当前管理员没有业务排程、修改或审批权限。"
  - admin 直接 `#/plans`：主区"无法打开" + 红框"管理员无业务数据权限，请进入系统说明"
- 证据：`CLEAN-T30-step1..4-{engineer-list,engineer-plan,admin-system,admin-plans-block}.{png,txt}`。
- 结果：**PASS**

### T31 — 搜索/筛选/历史
- 计划 URL：`http://127.0.0.1:18745/#/plans`、`http://127.0.0.1:18745/#/plan/5049675216bb`
- 动作：搜索 "CLEAN" → 列表缩小；搜索 "XYZ-no-match" → 0 条 + "暂无记录"；状态下拉"已批准" → 显示 2 条；打开 CLEAN-T05 → 滚到底"计算历史" → 点"查看历史计算" → 模态展开。
- 页面观察：模态标题"历史计算 · 72542d3ca70d"，状态"completed"，note"历史结果只读，不覆盖当前计划。"
- 证据：`CLEAN-T31-step1..7-{list-all,search-CLEAN,search-no-match,filter-approved,plan-history-area,history-modal,history-detail}.{png,txt}`。
- 结果：**PASS**

### T32 — 弹窗 Escape/Tab + 刷新持久化
- URL：`http://127.0.0.1:18745/#/plans`
- 动作：点"新建排程" → 模态打开 → 按 Escape → 模态关闭 → 重新打开 → 按 Tab ×3 → 焦点循环 → 点"取消" → 浏览器刷新。
- 页面观察：Escape 后页面回到列表；Tab 循环未越界。
- 证据：`CLEAN-T32-step1..5-{after-close-modal,modal-open,after-escape,after-tabs,after-reload}.{png,txt}`。
- 结果：**PASS**

### T33 — 主页截图
- URL：`http://127.0.0.1:18745/#/rules/F1%3Acase_00_micro_benchmark_12`、`http://127.0.0.1:18745/#/plan/1a09a138d80a/input`
- 动作：分别截图视口与整页。
- 证据：`CLEAN-T33-1-rules-{viewport,fullpage}.{png,txt}`、`CLEAN-T33-2-input-{viewport,fullpage}.{png,txt}`。
- 结果：**PASS**

### T34 — 切换已有草稿场景
- 动作：新建"CLEAN-T34-切场景" → 加载"正常生产 · 12辆基准" → 点"更换演示场景" → 点"取消选择" → 再次"更换演示场景" → 点"关键物料延期"。
- 页面观察：取消后 1·卡片仍显示"正常生产 · 12辆基准"；切换后变为"场景2：关键物料突发延期断料 (Supply Delay Stress)"，toast"演示输入已准备，后续步骤自动引用。"
- 证据：`CLEAN-T34-step1..4-{initial,scenario-list,after-cancel,after-switch}.{png,txt}`。
- 结果：**PASS**

### T35 — admin 直接 hash 越权
- 动作：admin → `#/plan/1a09a138d80a` 直接 hash → `#/inbox` 直接 hash。
- 页面观察：admin 直接 plan：主区"无法打开" + 红框"管理员无业务数据权限，请进入系统说明"。admin inbox：仅左侧导航"系统说明"。
- 证据：`CLEAN-T35-step1..2-{admin-direct-plan,admin-inbox}.{png,txt}`。
- 结果：**PASS**

### T36 — 已排未完成反例（150 min × 3 班次）
- 计划 URL：`http://127.0.0.1:18745/#/plan/169f1602e860`
- 动作序列：planner 新建 + 加载"正常生产 · 12辆基准" → engineer 把 3 个班次 end 改为 150 → 保存 → planner 引用最新 → 生成方案 → 逐候选点击展开"未完成订单"。
- 页面观察：
  - **4 候选全部可见"未完成订单"details**：
    - 交期优先：标题"未完成订单 · 4（未排0台，已排未完成4台）"，4 行 VIN_07/10/11/12，状态"已排未完成(已开工未完工)"，原因"已投入序列并开工但未能在计算时域内到达末道工序完工（在制未出线）"
    - 颜色集中、负荷均衡：同 EDD，4 台 VIN_07/10/11/12
    - 综合优化：3 台 VIN_05/11/12（不同候选未完工集合差异）
  - **未完工车辆工序时间证实缺末道 F**（`CLEAN-T36b-step5-process-vin07-10-11-12.png`）：
    - VIN_07 仅有焊装 W_LINE 180/180/195/195 + 涂装 P_LINE 262/272/307/307 两行（**无总装行**，缺末道 A 工序）
    - VIN_11 焊装 W_LINE 2925/2925/2940/2970（仅焊装一行，无涂装、无总装）
    - VIN_12 焊装 W_LINE 2970/2970/2985/—（**末列释放设备显"—"**，F 未到）
    - VIN_06 总装 A_LINE 2985/2985/3010/3010（已完成，作为对照）
  - 红框"不可提交：存在 4 台未完工订单（未排 0 台、已排未完工 4 台）：VIN_07、VIN_10、VIN_11、VIN_12"。"提交所选方案"按钮 disabled 样式。
  - 底部说明："按有效需求（净额后订单,已剔除取消且未开工）减真实完工完成集合判定；未排入与已排未完成都会阻断提交，系统不能造具体缺料归因。"
- 证据：`CLEAN-T36-step1..6-{plan-loaded,shifts-150-saved,refreshed,completed,*,*}.{png,txt}`、`CLEAN-T36b-step5-process-vin07-10-11-12.png`。
- 结果：**PASS**

---

## 5. 修复项复测（4/4 PASS）

| 修复项 | 复测证据 | 结果 |
|---|---|---|
| 加载态防旧身份闪现 | `CLEAN-FIX-LOAD-engineer-after-reload.png`：engineer reload 后 scope 仍为"工艺负责人 · F1"，未出现 planner-a 闪烁 | PASS |
| 候选锁定提交后无法更换 | `CLEAN-T21-step2-after-reload.png`：3·卡片直接展示锁定方案，无候选切换按钮 | PASS |
| 未完成明细拆分 | `CLEAN-FIX-UNSPLIT-metrics-after-rerun.png`、`CLEAN-T36-step5-交期优先.png`：指标网格与表格标题均拆分为"未排 X，已排未完成 Y"两段 | PASS |
| toast 4 秒自动消失 | `CLEAN-FIX-TOAST-2-toast-shown.png`：底部右下 toast"已恢复当前已保存版本。"可见；`CLEAN-FIX-TOAST-3-toast-hidden.png`：等待 5.5 秒后该区域已空，toast 已消失 | PASS |

---

## 6. 缺陷与边界

| ID | 严重度 | 描述 | 证据 | 状态 |
|---|---|---|---|---|
| BOUND-T01 | 低 | 验收库最初为空；复测时保留此前页面创建的测试计划 | `01-empty.{png,txt}` 主会话首测 | 已说明 |
| BOUND-T26 | 低 | 紧急事件场景：RUSH_ORDER 事件展示但页面未声称动态生效 | `CLEAN-T26-step3-completed.png` | 边界声明 |
| BOUND-T27 | 低 | ORDER_CANCEL 事件展示但未实际撤单；12 辆车仍按原序列完成 | `CLEAN-T27-step3-completed.png` | 边界声明 |

无未修复的严重/阻断缺陷。

---

## 7. 主数据状态

`F1:case_00_micro_benchmark_12` 已恢复基础值（班次 0-480、节拍 25/15/35/...）。版本号因历次保存有所提升。

---

## 8. 证据文件索引

证据目录：`/Users/huangrq25/Desktop/其他/Anio/aps-v2/acceptance-v2/clean/`

文件命名规则：
- `CLEAN-T{XX}-step{N}-{描述}.{png,txt}`
- `CLEAN-T{XX}-step{N}-{候选名}.{png,txt}`
- `CLEAN-T{XX}b-step{N}-{描述}.{png,txt}`（补验证据）
- `CLEAN-FIX-{项}-{描述}.{png,txt}`

共 372 文件（186 对 .png+.txt）。每条用例至少一对。

T01 引用：`/Users/huangrq25/Desktop/其他/Anio/aps-v2/acceptance-v2/01-empty.{png,txt}`（主会话首测真实空库 0 条）。

---

## 9. 结论

按硬约束"0 个未修复阻断/严重缺陷 + 100% 范围内用例执行且通过 + 全部覆盖修复项"：

- **36 = 34 普通 PASS + 2 边界 PASS**（T26/T27）。
- 4 项修复全部复测 PASS（toast 4s、身份无闪现、候选锁定、未完成明细拆分）。
- T36 "已排未完成"反例在 150 分钟 × 3 班次稳定构造；4 候选产生"已排未完成"分类；VIN_07/12 工序时间表证实缺末道 F 节点。
- T19 严格同 VIN_01 + 交期优先：tempo 25 → 总装 dur 25 (60→85)；tempo 40 → 总装 dur 40 (60→100)。
- 旧报告违规项保留不掩盖；本报告所有断言严格基于 DOM 可见文字。

**主会话审查结论：V2本轮页面范围内34项通过、2项边界展示验收通过，范围内准出；不等于原需求全部生产能力实现。** 已复核节拍前后工序时间、未完工提交阻断和计划链接证据；浏览器已 `task.finish({keep:[]})` 关闭 space7（页面无残留），服务进程继续运行，验收库所有数据保留。

## 10. 主会话证据审查补记

- 测试Agent最初报告中的CLEAN计划URL错误引用了旧测试计划；主会话已逐一依据clean目录对应loaded/input快照链接纠正，未读取数据库或调用接口核验。
- 测试Agent收尾额外使用curl检查静态首页连通性，不符合用户禁止接口式测试的要求，该结果不作为功能准出依据。CLEAN功能验收依据仅为真实页面操作的文本及图片。旧fetch辅助断言结果已否决，不混入本轮。
- T26/T27仅通过来源事件展示及边界说明验收；动态插单/撤单、在制保护仍未实现，不能以边界通过宣称功能实现。
