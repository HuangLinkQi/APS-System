# APS Fullchain 全链路仿真与离散事件计划重排系统 (`aps_fullchain_v1`)

本项目基于 Python 3 标准库（无外部依赖）实现了汽车混流装配全链路离散事件生产排产、仿真、审批、执行与滚动重排系统。

---

## 1. 核心架构与模块分工

```text
aps-fullchain/
├── schema.py              # 数据契约（aps_fullchain_v1）、时历、B/S/C/F四节点记录、快照数据模型
├── demand.py              # 需求净额与预测冲销（桶级冲销、守恒定律校验、虚拟车生成）
├── aggregate.py           # N+6/N+3/月度聚合计划（真实月份映射、零能力无回退、ID级守恒传递）
├── allocation.py          # 周/日逐车分配（支持跨日迁移、无4车绑定、冻区保护、释放时点硬约束）
├── policies.py            # 车间独立派工与三算法体系（Strategy A: EDD, Strategy B: 换色感知, Strategy C: 局部搜索领域变异）
├── simulator.py           # 离散事件仿真(DES)内核：B/S/C/F、正阻塞(F>C)、有限缓冲、分段人力预留(S==B+setup)、物理深复制
├── lifecycle.py           # 本地模拟计划审批、分工序派发与幂等回执确认、基线版本控制（无接受不执行）
├── rolling.py             # 状态快照提取、未来信息隔离、在制保护(WIP标CANCELLED_IN_WIP)、扰动前置与滚动重排
├── validate.py            # 独立审计校验器（QA独立维护，扫描13项物理与业务不变量，拦截非法反例）
├── evaluate.py            # 统一业务目标评价（零硬约束违反、拖期惩罚、阻塞工时、换色次数）
├── experiments.py         # 全闭环综合实验流水线、多候选评估、fail-closed门禁、TraceBundle全量导出
├── cli.py                 # 命令行交互工具（run 与 test 子命令）
├── fixtures/
│   └── scenarios.py       # 12车实单+4车预测微型基准算例（含正阻塞、拔出车、换色、跨日与物料到货）
└── tests/
    ├── test_g1_physics.py # G1门禁：手工可算物理测试（F>C、拔出车、S扣料）
    ├── test_g2_validator_counterexamples.py # G2门禁：独立校验器拒绝故意篡改的6类非法trace
    ├── test_g3_g4_end_to_end_rolling.py     # G3/G4门禁：端到端全流程与在制保护续算
    ├── test_g5_candidates_comparison.py     # G5门禁：A/B/C策略多候选评估与优选
    ├── test_independent_audit.py            # QA独立交付验收门禁（覆盖13项物理反例与边界条件）
    └── test_audit_fixes.py                  # 全链路阻断修复回归测试（覆盖7项反例与连续服务块机制）
```

---

## 2. 运行与验证命令

本项目严格使用 Python 3 标准库，无需 `pip install` 任何第三方包。

### 2.1 运行全部测试（共 71 项独立测试，含事件重排、技能独立反例与扩展套件）

在工作区根目录执行：
```bash
PYTHONPATH=. python3 -m aps_fullchain.cli test
# 或通过 pytest 直接运行：
PYTHONPATH=. pytest aps-fullchain/tests -v
```

### 2.2 运行全场景 x 全策略滚动闭环矩阵（9 Scenarios x 3 Strategies = 27 运行）

```bash
PYTHONPATH=. python3 -m aps_fullchain.cli run --all --out aps-fullchain/closed-loop-matrix-verified.json
```

### 2.3 运行动态事件滚动重排专用测试

```bash
PYTHONPATH=. pytest aps-fullchain/tests/test_event_replanning.py -v
```

### 2.4 运行单个全链路闭环算例并输出 TraceBundle

```bash
PYTHONPATH=. python3 -m aps_fullchain.cli run --scenario MICRO_BENCHMARK_12 --out aps-fullchain/trace_bundle.json
```

---

## 3. 核心机制落实与可核验证据

### 3.1 严格信息隔离与无泄漏
- **ExecutionWorld 与 Snapshot 分离**：`make_snapshot` 直接从物理运行态提取设备在制、剩余加工、缓冲队列与当前库存；
- **未来事件屏蔽**：`known_at_min > now_min` 的未来扰动事件及其 payload 对规划器严格不可见；
- **到账物料防重复计入**：`make_snapshot` 过滤已到账入库物料（记录于 `received_deliveries`），`known_deliveries` 仅保留未来且未到账批次，彻底杜绝规划层物料虚增双重计入。

### 3.2 离散事件两阶段批处理机制（Two-Phase Simultaneous Processing）
同一时间戳 $t$ 发生的事件分两阶段处理：
1. **Phase 1（批量释放）**：完工事件释放加工人力（Node C），准备结束触发加工转换，到货入库，转运入缓冲；
2. **Phase 2（原子申请与固定点推进）**：阻塞设备尝试推入下游缓冲（Node F），空闲设备按派工策略检查日历、人力、物料并启动准备（Node B）。

### 3.3 四节点、连续服务块与分段人力预留
- **四节点模型**：每一个工序显式记录 `B`（准备开始）、`S`（加工开始/领料）、`C`（加工完成/释放加工人力）、`F`（设备释放/工件进入缓冲或出线）；
- **分段精确人力预留**：在 Node B 准入时，对 $[B, B+setup)$ 预留 $l_{setup}$，对 $[B+setup, C)$ 预留 $l_{proc}$，联合全部已承诺区间进行并发容量切片核验，原子提交。保证 $S \equiv B + setup\_time$ 严格准时发生，绝无中间等待，且不因保守预留而人为压缩车间并行产能；
- **正阻塞（Positive Blocking, $F > C$）**：下游工序慢于上游且中间缓冲容量满时，上游设备完工后保持占用（BLOCKED），实测准确捕捉多次正阻塞；下游开始准备时原子释放缓冲位，级联唤醒受阻设备。

### 3.4 拔出车（Pull-out Vehicle）特殊路径
- 拔出车（`is_pullout=True`）工艺路径仅含 `["W", "P"]`；
- 在涂装工序完工（$C_P$）后立即退出产线，不占用涂装-总装缓冲（`BUF_PA`），不进入总装车间（A），不消耗总装电池与座椅物料。独立校验器对任何拔出车进入 A 工序的行为进行硬拦截。

### 3.5 滚动再计划与事件决策屏障（Rolling Re-planning with Decision Barrier）
- **事件获知决策屏障**：当仿真推进到外部事件获知时刻 `known_at_min` 时，DES 引擎执行完 Phase 1（已发生完工释放、实际到货、停线、减员、取消等物理事实更新生效），并在 Phase 2（派工新准备 Node B）之前设置**决策屏障立即暂停**；
- **重排与层级重算**：在屏障处提取隔离快照，执行需求重新冲销（守恒校验）、N+6/N+3 聚合计划重算（反馈完工与在制方差，核验重合月一致性）、周/日逐车分配重算（未开工车辆允许跨日迁移，在制车辆冻结保留）；
- **审批接收与同 World 续算（前缀不变）**：生成子版本（v+1）提交生命周期管理器，审批通过并完成派发回执后，仅将新分配注入未开工车辆（`UNRELEASED` / `READY`），同一 `ExecutionWorld` 物理世界从该屏障时刻直接续算，$[0, \text{known\_at}]$ 历史事件前缀绝对不删、不改、不重跑。

### 3.6 MATERIAL_DELAY 真实延迟机制与仲裁规则
- **未入库批次真实推迟**：延迟事件更新物理未到货批次的 `available_at_min`，并遍历事件堆清除旧队列中的到货事件，重新调度至新时刻，杜绝双重到货与虚假早到；
- **已入库物料不可撤销**：若批次已真正入库（已在 `received_delivery_ids`），延迟请求被明确拒绝并记录审计日志，不撤销已入库物理库存；
- **同刻到货与延迟确定性仲裁**：同一时刻 $t$ 同时发生到货与延迟时，延迟事实先于到货执行，在入库前拦截并推迟批次，杜绝偶发队列顺序依赖；
- **规划快照先知隔离**：$t=0$ 快照仅见原承诺供货时间，到达 `known_at_min` 后的快照方可见延期，严格杜绝向规划器泄漏未来真实供货。

### 3.7 真实三算法体系（Strategies A, B, C）
- **Strategy A (EDD)**：全工序纯交期优先派工规则；
- **Strategy B (ColorAware)**：P 车间颜色感知批次连喷（消除换色清洗），A 车间车型平衡派工；
- **Strategy C (Local Search / Sequence-Guided)**：基于领域变异算子（2-opt 交换与块倒序）探索不同投产序列，并由 `PlanSequenceDispatchPolicy` 精确驱动物理派工，产生真正差异化的物理完工与阻塞指标。

### 3.8 审批回执与无接受不执行（No Acceptance, No Execution）
- 滚动重排生成的重排计划（OrderPlan v2）必须提交至生命周期管理器，经过审批（APPROVED）并分工序派发确认（RECEIPT_CONFIRMED）；
- 若审批被驳回或回执未确认，系统自动 fail-closed，严禁在物理世界执行未经确认的计划，现场维持原计划。

### 3.9 技能池容量变点切片与严格输入校验
- **多工序技能池**：支持 `LaborSkillPool` 独立容量上限及动态时间窗（`time_windows`）；
- **容量变点全切片检查**：`can_reserve_labor` 在区间端点、班次边界与全部技能/总量容量变动点进行细粒度切片核验；
- **严格输入拒绝**：`normalize_scenario` 在场景准入时即刻校验工序所需技能，遇到未在技能池声明的未知技能直接报错拒绝，不静默归零。

### 3.10 物理初态不可变（Scenario Immutability）
- `ExecutionWorld.__init__` 对输入的 scenario 及其 resources、calendar、buffers 等对象执行完整深度复制（`copy.deepcopy`），物理运行决不污染原始 scenario 状态，确保校验器能够基于真实的静态初态独立完成审计。

---

## 4. 实施状态与边界说明（严格区分）

### 4.1 已实现能力
1. **完整闭环数据流**：需求冲销 $\to$ 聚合供需 $\to$ 日逐车分配 $\to$ 车间独立派工 $\to$ 四节点DES仿真 $\to$ 本地审批与幂等回执 $\to$ 物理执行 $\to$ 动态事件 $\to$ 在制保护重排；
2. **守恒与逐ID实际传递**：原始预测(16) = 冲销量(12) + 剩余量(4)；净需求(16) = 实单(12) + 虚拟车(4)；聚合计划逐ID传递，全部16辆车均真实参与排产与仿真执行；
3. **真实正阻塞与有限缓冲**：纯离散事件时间步推进，实测捕捉 $F > C$ 阻塞工时；
4. **车间独立派工与多策略优选**：Strategies A, B, C 具备独立派工决策与差异化结果，选出最低惩罚方案；
5. **在制保护**：扰动后前缀轨迹不变，续算而不是重跑；
6. **可审计 TraceBundle**：导出全量物理事件流、各检查点快照、库存变动轨迹、人力占用轨迹与缓冲区占用轨迹。

### 4.2 简化假设（部分实现）
1. **微型工厂基准**：当前基准案例为 12 辆实单 + 4 辆虚拟车、3 个工序阶段、3 天工作窗口（为便于手工核算与即时验证）；全链路接口已参数化支持规模扩展；
2. **单一共享人员池（Single Shared Labor Pool）**：当前 `max_workers` 与分段人力预留验证的是全厂单一共享人员容量约束，**不等于**多工序异构技能矩阵池（Multi-skill Matrix Pool）；
3. **本地模拟审批与接收**：采用内存状态机与哈希幂等键模拟制造运营系统（MOM/MES）接口，未对接真实外部企业级 ERP/MES 系统；
4. **无全局最优性证明**：采用确定性启发式与多邻域局部搜索策略，给出真实可落地的可行解与指标对比，不宣称数学全局最优。

### 4.3 未实现能力与未覆盖边界
1. **未覆盖工艺细节**：不含冲压车间模具更换细化热平衡、涂装烘烤炉温区流体物理、总装滑板线积放输送链物理摩擦等底层机理；
2. **未覆盖多技能工种矩阵**：未建模电工、焊工、质检等工种细分资质与工位技能绑定；
3. **未覆盖电池电芯分选**：电池作为高阶装配件按 BOM 物料管理，未展开至电芯分容压差匹配；
4. **未经现场标定**：工时、换色清洗时间与缓冲容量为基准设参，不代表特定整车工厂实测工艺标定值。
