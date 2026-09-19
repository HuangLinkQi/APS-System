# APS测试集XML用例库与接口实例说明文档

## 1. 概述

本目录（`test-suites-xml/`）归档了APS 全链路排产仿真与算法评测的全部场景用例。所有用例均已转为工业标准 XML 报文格式（基于 `ApsInterfaceSchema.xsd` 定义），模拟车企上位 ERP（如 SAP）、MES、WMS 与 SCM 系统向 APS 下发生产调度与外部扰动的接口实例。

每个 XML 文件均代表一次完整的“接口实例（Interface Payload Instance）”，具备严格的消息头（MessageHeader）、日历班次、工位资源拓扑、有限缓冲区、人员技能池、期初库存、在途送货、BOM 配置、生产工单以及外部动态扰动事件。

---

## 2. 接口实例用例清单与分类

本用例库共包含 **25 个标准 XML 接口实例**，分为三大类别：

### 2.1 核心业务基准与单项扰动用例（10 个）

| XML 文件名 | 对应场景标识 | 核心测试焦点与物理业务机制 |
| :--- | :--- | :--- |
| `case_00_micro_benchmark_12.xml` | `MICRO_BENCHMARK_12` | 12 车微型物理验证基准，验证四节点 $B/S/C/F$ 与正阻塞触发。 |
| `case_01_normal_baseline.xml` | `SCENARIO_1_NORMAL` | 标准平稳连续混流生产，双班制作业，验证基准排产与守恒律。 |
| `case_02_supply_delay.xml` | `SCENARIO_2_SUPPLY_DELAY` | 关键物料延迟到货（t=250 到账），验证安全库存吸收与零拖期（TWT=0）。 |
| `case_02_supply_delay_stress.xml` | `SCENARIO_2_SUPPLY_DELAY_STRESS` | 关键物料延期重压（期初库存缩减），验证总装断料停工与正拖期（TWT>0）。 |
| `case_03_material_shortage.xml` | `SCENARIO_3_MATERIAL_SHORTAGE` | 严重缺料场景（电池仅 8 台），验证 7 辆缺料车门禁拦截未排及优雅退出。 |
| `case_04_rush_order_insertion.xml` | `SCENARIO_4_RUSH_ORDER` | 紧急 VIP 加急插单，验证同桶未开工虚拟预测车等量冲销与需求守恒。 |
| `case_04_rush_tight_due.xml` | `SCENARIO_4_RUSH_TIGHT_DUE` | 紧交期突发插单（非冲销），需求扩充至 17 辆，验证排队挤压与拖期传导。 |
| `case_05_order_cancellation.xml` | `SCENARIO_5_CANCELLATION` | 订单撤销冲击：未开工订单撤单授权撤销，在制订单打标在制保护持续执行。 |
| `case_06_line_stop_labor_reduction.xml` | `SCENARIO_6_RESOURCE_LABOR_DISRUPTION` | 涂装停线 40 分钟与人员缩减至 1 人，验证日历时窗与技能池并发保护。 |
| `case_07_cross_month_planning.xml` | `SCENARIO_7_CROSS_MONTH` | 跨月度规划（10/11/12月），验证无班次月份零产能与累积缺料准入拦截。 |

### 2.2 多规模扩展与工艺复合扰动用例（12 个）

涵盖 16 车（单日）、32 车（双日）、48 车（三日）三种跨度与 4 种严苛复合工况：

| XML 文件名 | 规模 | 复合扰动类型与业务特征 |
| :--- | :---: | :--- |
| `adv_scenario_16_normal.xml` | 16车 | 单日 16 车基准混流，验证轻量级流水线流动。 |
| `adv_scenario_16_bottleneck_color_alum.xml` | 16车 | 铝车身超 50% + 5 色高频交替，验证涂装小颜色工艺瓶颈。 |
| `adv_scenario_16_dual_delay_labor_cut.xml` | 16车 | 双物料到货延期（电池+座椅）与人员减半复合冲击。 |
| `adv_scenario_16_vip_rush_cancel_shock.xml` | 16车 | 连续 2 辆 VIP 加急插单 + 未开工订单撤销复合冲击。 |
| `adv_scenario_32_normal.xml` | 32车 | 双日 32 车跨日混流，验证工作日边界平稳过渡。 |
| `adv_scenario_32_bottleneck_color_alum.xml` | 32车 | 双日工艺双瓶颈，换色次数呈非线性激增，验证颜色聚类优化。 |
| `adv_scenario_32_dual_delay_labor_cut.xml` | 32车 | 双日物料人员双重延误，验证在线重排与在制工单继承。 |
| `adv_scenario_32_vip_rush_cancel_shock.xml` | 32车 | 双日大扰动冲击，验证队列派工置换与稳定性控制。 |
| `adv_scenario_48_normal.xml` | 48车 | 三日 48 车大规模混流，验证工业级排程吞吐性能。 |
| `adv_scenario_48_bottleneck_color_alum.xml` | 48车 | 极端工艺瓶颈：验证 EDD 换色 44 次 vs ALNS 换色 21 次的极限差距。 |
| `adv_scenario_48_dual_delay_labor_cut.xml` | 48车 | 大规模多点物资受限，验证动态重排的连续可执行性。 |
| `adv_scenario_48_vip_rush_cancel_shock.xml` | 48车 | 大规模插单与撤单冲击，验证复杂装配序列的扰动抗性。 |

### 2.3 接口健壮性与逆向反例用例（3 个，用于接口层拦截测试）

| XML 文件名 | 注入非法属性 | 预期接口层处理与拦截行为 |
| :--- | :--- | :--- |
| `invalid_case_01_negative_material.xml` | 期初物料库存注入 `-50` | 接口层 XSD 模式校验或准入层直接拦截，拒绝负库存报文。 |
| `invalid_case_02_zero_buffer.xml` | 工序间缓冲容量配置为 `0` | 阻断排产生成，报告车间无中转容积、物理不可达错误。 |
| `invalid_case_03_unregistered_config.xml` | 工单关联未注册配置 `UNREGISTERED_CFG_999` | 准入层核对 BOM 配置主数据失败，订单被隔离不进入排产。 |

---

## 3. 报文结构与解析映射规则

每个 XML 根节点为 `<ApsInterfacePayload>`，严格遵循以下结构映射：
1. `<MessageHeader>`：提供消息跟踪 ID、数据哈希及事务时间戳。
2. `<FactoryCalendar>`：定义车间班次（Shift）、开工与停线保养时段。
3. `<ShopResources>`：声明焊装（W_LINE）、涂装（P_LINE）、总装（A_LINE）设备及初始颜色/车型。
4. `<InterStageBuffers>`：声明工位间缓冲容量（如 `BUF_WP` 容量 2，`BUF_PA` 容量 2）及运输时间。
5. `<LaborSkills>`：声明总定员及焊装、涂装、总装技能池划分。
6. `<InitialInventory>`：声明物理安全库存。
7. `<SupplyDeliveries>`：声明供应商送货计划与预计可用时点。
8. `<VehicleConfigurations>`：声明车型（SUV/SEDAN）、车身结构（STEEL/ALUMINUM）、各工段节拍与物料 BOM。
9. `<ProductionOrders>`：声明生产工单、唯一车架号（VIN）、优先级及交期。
10. `<ExternalEvents>`：声明生产过程中到达的动态事件（如停线、到货调整、撤单）。

---

## 4. 验证与加载指令

可以使用标准 Python 脚本对本目录下所有 XML 报文进行批量反向解析与 XSD 语法校验：

```bash
# 验证所有 XML 用例格式与结构完整性
python3 -c "
import xml.etree.ElementTree as ET
from pathlib import Path
xml_files = list(Path('/Users/huangrq25/Desktop/其他/Anio/test-suites-xml').glob('*.xml'))
for p in xml_files:
    tree = ET.parse(p)
    root = tree.getroot()
    assert root.tag == 'ApsInterfacePayload'
print(f'Verified {len(xml_files)} XML interface test cases successfully!')
"
```
