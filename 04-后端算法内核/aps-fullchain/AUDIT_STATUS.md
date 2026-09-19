# APS Fullchain 审核状态与系统边界说明文档 (`AUDIT_STATUS.md`)

本文档对 `aps_fullchain_v1` 当前工程落盘状态、已通过的审计门禁、以及**明确未实现的企业级边界**进行公开、真实的声明，严格遵循科研与工程严谨性原则，不把微型可核验算例夸大为全厂实测能力。

---

## 1. 修复完成度与门禁验收总览

系统在全链路通过了由独立 QA 团队构建的 25 项物理审计门禁、前序补充的 12 项反例测试、以及本轮扩展的 11 项全场景与跨期测试，全套测试套件共计 **59 项测试用例全部通过（59/59 PASS）**。

| 门禁与特性编号 | 问题与需求说明 | 修复机制与物理保证 | 验证测试用例 |
| :--- | :--- | :--- | :--- |
| **增量 1：分层与跨期** | 独立 N+6 / N+3 对象、重合月一致、锁定/完成继承、跨月归属、执行反馈新版本 | 实现 `plan_n_plus_6`, `plan_n_plus_3`, `verify_overlap_consistency`；实现 `generate_variance_replan_aggregate`（继承 `locked_order_ids`/`completed_order_ids`，生成 v2 及父版本指针）；记录 `cross_month_attributions`；无排班月份严格 0 产能 | `test_seven_scenarios_and_extensions.py::test_n6_n3_*`, `test_locked_*`, `test_cross_month_*` |
| **增量 2：技能池与日历** | 命名技能池、按时段容量、准备加工分别预留 | 实现 `LaborSkillPool`（`WELD_SKILL`, `PAINT_SKILL`, `ASSY_SKILL`, `GENERAL`）及时段容量 `time_windows`；工序分别配置 `skill_requirements`；Node B 准入原子校验技能池及全厂总人力 | `test_seven_scenarios_and_extensions.py::test_named_skill_pools_*` |
| **增量 3：全矩阵闭环** | 7 基准场景 + 2 压力变体 x 3 策略（共 27 运行）全闭环执行与校验 | 全部 27 个独立运行均在物理仿真中真实执行完毕，且 100% 通过独立校验器（27/27 `is_valid: YES`，0 物理违规） | `test_seven_scenarios_and_extensions.py::test_all_scenarios_matrix_*` |
| **增量 4：延迟与紧交期** | 区分安全库存吸收（TWT=0）与缺料饥饿拖期（TWT>0） | 原 `SUPPLY_DELAY`（8台库存，延期到250）被安全库存吸收，TWT=0 属真实物理现象保留为基准；新增 `SUPPLY_DELAY_STRESS`（2台库存，延期到250），准确在 Node B 发生停工等待，产生真实 TWT | `test_seven_scenarios_and_extensions.py::test_supply_delay_buffer_absorption_vs_stress_starvation` |
| **增量 5：插单冲销边界** | 动态插单纳入分母，同桶未投产虚拟车冲销防双算；在制保护 | 插单匹配同桶未投产虚拟车进行等量替换冲销（总需求守恒为 16）；已投产在制虚拟车严格不可删除，打上 `CANCELLED_IN_WIP` 保护物理执行；紧急超额插单 `RUSH_TIGHT_DUE` 显式扩充需求至 17 且紧交期产生 TWT | `test_seven_scenarios_and_extensions.py::test_rush_order_netting_*` |
| **增量 6：真实稳定性** | 摒弃占位 0，度量共同未开工车辆的排产扰动 | 过滤新增与取消订单，仅度量前后基线共同未开始车辆的日历位移（`day_shift_count`）与工序调度逆序数（`inversion_count`，Kendall-tau对数） | `test_seven_scenarios_and_extensions.py::test_stability_kendall_tau_*` |
| **增量 7：参数化字典序** | 恢复未排数优先，首车换色统计，零分母保护 | 字典序恢复 Level 1 未排数优先（`unmet_count`）$\to$ Level 2 未排权重 $\to$ Level 3 完工拖期 $\to$ Level 4 换色 $\to$ Level 5 阻塞 $\to$ Level 6 完工时间；首车相对设备初态颜色计入换色；活跃需求为 0 时 `service_rate = None` | `test_seven_scenarios_and_extensions.py::test_initial_color_*`, `test_zero_denominator_*` |
| **阻断 1** | `aggregate.py` 复制首月能力、0班次回退20天、30天硬编码、超horizon需求塞首月 | 采用 `calendar.monthrange` 计算真实月份天数；无排班月份能力严格为零；超horizon需求准入拒绝；ID级双向守恒传递 | `test_audit_fixes.py::test_1_*` |
| **阻断 2** | `make_snapshot` 重复带上已到账 deliveries 导致库存二次虚增 | 物理到账（$t \le now\_min$）归入 `received_deliveries`，`known_deliveries` 严格仅保留未来未到账批次 | `test_audit_fixes.py::test_2_*` |
| **阻断 3** | `rolling.py` 取消事件在快照后生效；在制车被标 CANCELLED 导致保护矛盾 | 扰动事件前置于物理 world 生效；在制打上 `CANCELLED_IN_WIP` 标签保持执行；未开工标 `CANCELLED` 并排除在重排外 | `test_audit_fixes.py::test_3_*` |
| **阻断 4** | `experiments.py` 无有效候选仍盲批；遗漏 synthetic 车辆（声称16实跑12） | 设立 Fail-Closed 硬门禁，无有效解直接抛异常阻断；冲销生成的 4 辆虚拟车全流程参与排产与物理执行（16车全守恒） | `test_audit_fixes.py::test_4_*`, `test_5_*` |
| **阻断 5** | `replan` 直接更新 world 执行，缺少新版本审批回执 | 滚动重排生成 OrderPlan v2，强制经过审批与分工序幂等回执，落实“无接受不执行（No Acceptance, No Execution）” | `test_audit_fixes.py::test_6_*` |
| **阻断 6** | `trace_bundle` 仅导摘要，缺乏可审计物理证据 | 全量导出事件流水、多检查点物理快照、库存时序轨迹、人力占用轨迹与缓冲区时序轨迹 | `test_audit_fixes.py::test_7_*` |
| **阻断 7** | 服务块连续性与假三算法判定 | 实现精细区间人力预留保证 $S \equiv B + setup\_time$ 无缝衔接；实现基于 2-opt 变异算子和序列派工策略的真实 Strategy C | `test_audit_fixes.py::test_8_*`, `test_10_*` |
| **初态保护** | 仿真浅拷贝污染原始输入 scenario 状态 | `ExecutionWorld.__init__` 对 scenario 结构做全量 `copy.deepcopy`，物理执行零污染静态初态，供独立校验器可信审计 | `test_audit_fixes.py::test_9_*` |

---

## 2. 当前已验证的物理与算法能力

1. **离散事件四节点仿真 (DES Kernel)**：
   - 显式建模 `B`（准备开始）、`S`（加工开始/领料）、`C`（加工完成/释放加工人力）、`F`（工件出站/设备释放）；
   - 精细分段人力预留：在 Node B 准入时对 $[B, B+setup)$ 预留 $l_{setup}$，对 $[B+setup, C)$ 预留 $l_{proc}$，时间切片联合校验并发上限，原子提交。保证 $S \equiv B + setup\_time$ 严格准时发生，同时避免全段保守预留导致的车间并行能力压缩；
   - 捕捉下游瓶颈导致的正阻塞（Positive Blocking, $F > C$）；下游准备释放缓冲时级联唤醒（Cascading Unblock）；
   - 拔出车（Pull-out Vehicle）在涂装工序后正常出线，旁路总装缓冲与总装车间。

2. **端到端闭环数据流**：
   - 桶级预测冲销 $\to$ 月度聚合计划 $\to$ 周/日逐车分配（支持跨日迁移与冻区锁定） $\to$ 车间独立派工 $\to$ 四节点仿真 $\to$ 计划审批与分工序派发回执 $\to$ 扰动中断 $\to$ 快照隔离 $\to$ 在制保护重排 $\to$ 独立校验器核验。

3. **需求完整守恒**：
   - 原始预测(16) = 冲销实单(12) + 剩余预测(4)；
   - 净需求(16) = 实单(12) + 虚拟车(4)；
   - 全流程 16 辆车（12 实单 + 4 虚拟车）完整参与日分配与物理仿真；在扰动取消 1 辆未开工实单（VIN_11）后，活跃需求 15 辆，完工 15 辆，达成 100% 实际履约率。

---

## 3. 明确未实现的企业级边界与声明

为防范科研浮夸，以下内容**明确声明为简化假设或未实现能力**，不得在汇报或论文中宣称为“已在整车厂落地实测”：

1. **技能池与人员时段边界 (Labor Skill Pools & Time Window Scope)**：
   - 当前已实现工序与准备/加工分阶段的命名技能绑定（`WELD_SKILL`, `PAINT_SKILL`, `ASSY_SKILL`, `GENERAL`）及人员时段容量波动（`time_windows`）；
   - **未实现**工人个体级别的跨工种技能评级（Skill Level 1-5）、工位资质认证（Station Certification）、班次轮岗考勤排班与疲劳度恢复机理。

2. **固定邻域搜索边界 (Fixed Neighborhood Search Scope)**：
   - Strategy C 明确命名为“固定邻域（Fixed Neighborhood）”，基于 2-opt 对换与块翻转的确定性邻域候选驱动物理派工；
   - **不虚称**变邻域搜索（VNS）或自适应大邻域搜索（ALNS），未实现基于接受准则（Metropolis/SA）与邻域扰动抖动算子的元启发式迭代循环。

3. **月度中长期规划与多周期边界 (Aggregate Planning & Multi-period Scope)**：
   - 当前已实现独立的 N+6 与 N+3 计划对象建模、重合月一致性校验、已锁定/已完成订单继承、跨月归属跟踪与执行差异反馈生成新版本（v2+父版本指针）；
   - 长期层采用可审查的整数增量贪心准入；**未调用**基于商用求解器（Cplex/Gurobi/SCIP）的 MILP 多周期动态规划全局优化。

4. **系统对接与接口边界 (Integration Scope)**：
   - 审批流与回执确认采用内存状态机与 SHA-256 幂等签名模拟；
   - **未对接**真实外部企业级 ERP（如 SAP）、MOM/MES（如 Siemens Opcenter）或 WMS/LES 物流执行系统。

5. **底层工艺物理机理边界 (Process Physics Scope)**：
   - 不包含冲压车间大型模具更换液压与温度平衡曲线；
   - 不包含涂装烘烤炉温区流体动力学与升降温曲线；
   - 不包含总装滑板积放输送链机械摩擦力与电机节拍同步细节；
   - 电池作为整件 BOM 扣减，未展开至电芯分容压差筛选与模组并串联一致性物理匹配。

6. **规模与标定说明 (Scale & Calibration)**：
   - 实验基准基于 16 辆车、3 个主要工序阶段、3 天跨度的微型算例；工时与工艺参数用于逻辑正确性与数学不变量校验，不代表特定整车工厂的现场工时标定。
