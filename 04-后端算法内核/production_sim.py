"""production_sim.py — 生产量级等效瓶颈流水线离散事件仿真（秒级、真实自然日班历，非工位数字孪生）。

范围与边界（用户已拍板；仅本文件，勿改 server/web）：
- 秒级等效瓶颈流水 DES：3 车间 (W焊装 / P涂装 / A总装) 串行，车间间并行 + 有限缓冲，正阻塞不丢车。
- 明确假设：每车间 = 单台产能槽（瓶颈口径）；本文件是"瓶颈流水等效模型"，不是多工位数字孪生，不冒充工厂实测精度。

物理/经济模型（本次修订要点）：
- 真实自然日历：5 自然日（每 86400s），每天两个 460min 工作窗口
  （例 06:00-13:40、14:20-22:00），horizon = 第5天22:00。
  加工在非工作窗口"暂停顺延"；到货 / 故障 / 订单释放 / 交期一律用同一自然日墙钟口径。
  自然日与工作窗口量纲严格分离（若天然=86400 秒，双班 920min 只是工作窗口，不混为数轴）。
- BOM 按配置消费：W 消耗 STEEL/ALUM（按 config.raw），P 消耗 PAINT，A 消耗 BATTERY+MOTOR。
- 正阻塞：上游完工而下游缓冲满 → 上游 blocked 持有该车（占产能槽）直到下游取走；不丢车、无负库存。
- 故障：每线独立 Poisson 故障台数（指数型 MTBF 到达）＋随机修复时长；可为零；故障窗为墙钟。
- 换型换色：P 按颜色变换计 switch_sec；W/A 按车型变换计 switch_sec（三车间 last_attr 均在开工时更新）。
- 随机量一次性抽样 → 同一 Reality 供全部策略复用（CRN 公平）。
- 队列语义：选择阶段只读（绝不预删除）；物料校验通过后才出队（W 惰性标记、P/A 从缓冲移除）。
  P/A 只按策略键选最优，W 的 OPTIMIZED 走固定序列堆，两堆统一按"已开工 W 集合"惰性删除，防重复/遗漏。
- 死循环防护：时间/状态推进每一步都做推进性校验；事件总数、同一时刻事件数、_refresh 状态迁移次数
  均设硬上限，超限抛错（样本失败），不静默停在 0 进度。
- 物理不变量独立校验（唯一开工 / 工序先后 / 库存非负 / 物料台账守恒 / 缓冲上限 / W→P 与 P→A 流守恒不丢车 /
  完成口径会计）：任何违规 → 该样本 failed 并计入 errors，final_status 不得为 complete。
- horizon 内未完工：finish 在时间线中展示为 null（JSON 不允许 Infinity），未完工事件本身仍保留 inf。

统计与准出（用户明确）：
- final_status 仅在全部样本执行完成且无 error 时为 complete；任何 error → not_complete 且保留 error。
- conditional_on_time_rate 在 completed==0 返回 None（非 0），统计记 n_valid / n_total。
- mean_tardy 两个正交口径并在统计与文档单列：
    mean_tardy_over_completed_sec = 拖期和 / 已完成台数
    mean_tardy_over_late_sec     = 拖期和 / 拖期台数
- 每策略聚合 mean/std/p05/p95/95%CI(正态近似) 。

仅标准库。
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量（用户给定）
# --------------------------------------------------------------------------- #
SAMPLE_ALG_VERSION = "prod_sim_v1"

NATURAL_DAY_SEC = 86400.0
WORKDAYS = 5
SHIFTS_PER_DAY = 2
SHIFT_MIN = 460.0                  # 每班 460 分钟（用户给定）
SEC_PER_MIN = 60.0
SHIFT_SEC = SHIFT_MIN * SEC_PER_MIN                 # 27600 s

# 每日两个工作窗口（绝对分钟/秒，相对每日 0 点）
SHIFT1_START_START_SEC = 6 * 3600.0              # 06:00
SHIFT1_END_SEC = SHIFT1_START_START_SEC + SHIFT_SEC          # 13:40
SHIFT2_START_START_SEC = 14 * 3600 + 20 * 60     # 14:20
SHIFT2_END_SEC = SHIFT2_START_START_SEC + SHIFT_SEC          # 22:00


def _make_windows() -> List[Tuple[float, float]]:
    win = []
    for d in range(WORKDAYS):
        base = d * NATURAL_DAY_SEC
        win.append((base + SHIFT1_START_START_SEC, base + SHIFT1_END_SEC))
        win.append((base + SHIFT2_START_START_SEC, base + SHIFT2_END_SEC))
    return win


HORIZON_SEC = _make_windows()[-1][1]              # 第5天22:00（绝对值）

STAGES = ("W", "P", "A")
STRATEGIES = ("EDD", "COLOR_GROUP", "LOAD_BALANCE", "OPTIMIZED")

# 数值与安全阈值
_EPS = 1e-9                  # 秒级浮点容差（推进残差）
_TIME_EPS = 1e-6             # 事件时间比较容差
_STOCK_EPS = 1e-9            # 库存非负容差
_SAME_TIME_EVENT_LIMIT = 20000   # 同一时刻事件数硬上限（防同刻事件无限重复）
_REFRESH_LOOP_LIMIT = 256        # 单次 _refresh 内状态迁移次数硬上限


# --------------------------------------------------------------------------- #
# 工作日历
# --------------------------------------------------------------------------- #
class WorkCalendar:
    """真实自然日班历。加工只能在窗口内进行，窗口外暂停顺延。"""

    def __init__(self, windows: Optional[List[Tuple[float, float]]] = None):
        self.windows = sorted(windows if windows is not None else _make_windows())
        self.horizon = max(e for _, e in self.windows)

    def in_work(self, t: float) -> bool:
        return any(s <= t < e for s, e in self.windows)

    def work_end(self, t: float) -> Optional[float]:
        for s, e in self.windows:
            if s <= t < e:
                return e
        return None

    def next_work_start(self, t: float) -> float:
        for s, e in self.windows:
            if e > t:
                return s if s > t else t
        return self.horizon

    def to_dict(self):
        return {
            "natural_day_sec": NATURAL_DAY_SEC,
            "workdays": WORKDAYS,
            "shifts_per_day": SHIFTS_PER_DAY,
            "shift_min": SHIFT_MIN,
            "windows": [[round(s, 2), round(e, 2)] for s, e in self.windows],
            "horizon_sec": self.horizon,
        }


# --------------------------------------------------------------------------- #
# 领域对象
# --------------------------------------------------------------------------- #
@dataclass
class Order:
    vin: str
    config: str
    release_at: float      # 绝对秒（墙钟）
    due_at: float
    priority: int
    model: str
    color: str

    def to_dict(self):
        return {"vin": self.vin, "config": self.config,
                "release": round(self.release_at, 2), "due": round(self.due_at, 2),
                "priority": self.priority, "model": self.model, "color": self.color}


@dataclass
class ConfigDef:
    config_id: str
    model: str
    color: str
    raw: str               # "STEEL" / "ALUM"
    switch_sec: float      # 换色/换型准备秒（仅在属性变更时计入）
    is_pullout: bool = False
    process_sec: Dict[str, float] = field(default_factory=dict)   # {W,P,A}

    def bom(self, stage: str) -> Dict[str, float]:
        if stage == "W":
            return {self.raw: 1.0}            # STEEL / ALUM
        if stage == "P":
            return {"PAINT": 1.0}
        if stage == "A":
            return {"BATTERY": 1.0, "MOTOR": 1.0}
        return {}

    def to_dict(self):
        return {"config_id": self.config_id, "model": self.model, "color": self.color,
                "raw": self.raw, "switch_sec": self.switch_sec,
                "is_pullout": self.is_pullout, "process_sec": self.process_sec}


@dataclass
class SupplyDelivery:
    batch_id: str
    material: str          # STEEL / ALUM / PAINT / BATTERY / MOTOR
    qty: float
    planned_at: float      # 绝对秒（墙钟）

    def to_dict(self):
        return {"batch_id": self.batch_id, "material": self.material,
                "qty": self.qty, "planned_at": round(self.planned_at, 2)}


@dataclass
class ProductionScenario:
    scenario_id: str
    orders: List[Order]
    configs: Dict[str, ConfigDef]
    deliveries: List[SupplyDelivery]
    initial_stock: Dict[str, float]
    buffer_cap: Dict[str, int]               # {"W->P", "P->A"}
    pullout_vins: List[str]
    calendar: WorkCalendar = field(default_factory=WorkCalendar)

    @property
    def pullout_set(self):
        return set(self.pullout_vins)

    def to_dict(self):
        return {
            "scenario_id": self.scenario_id,
            "n_orders": len(self.orders),
            "calendar": self.calendar.to_dict(),
            "configs": {k: v.to_dict() for k, v in self.configs.items()},
            "deliveries": [d.to_dict() for d in self.deliveries],
            "initial_stock": dict(self.initial_stock),
            "buffer_cap": dict(self.buffer_cap),
            "n_pullout": len(self.pullout_vins),
        }


# --------------------------------------------------------------------------- #
# Reality —— 单扰动抽样（CRN：同一 Reality 供全部策略）
# --------------------------------------------------------------------------- #
@dataclass
class Reality:
    sample_seed: int
    delays: Dict[str, float]
    proc_multiplier: Dict[Tuple[str, str], float]
    failure_windows: Dict[str, List[Tuple[float, float]]]
    params: Dict[str, object] = field(default_factory=dict)

    def to_dict(self):
        hashlib = __import__("hashlib")
        # 用完整 vin 与 stage 拼接键，避免首字符截断/歧义
        final_input = "".join(f"{v!r}|{s!r}|{m:.6f};"
                              for (v, s), m in sorted(self.proc_multiplier.items(),
                                                      key=lambda kv: (kv[0][0], kv[0][1])))
        return {
            "sample_seed": self.sample_seed,
            "delays": {k: round(v, 2) for k, v in self.delays.items()},          # 完整数值
            "proc_multiplier_sha256": hashlib.sha256(final_input.encode()).hexdigest(),
            "failure_windows": {st: [[round(a, 2), round(b, 2)] for a, b in wl]
                                for st, wl in self.failure_windows.items()},
            "alg_version": SAMPLE_ALG_VERSION,
            "params": dict(self.params),
            "reproducible_via": "sample_seed + params + alg_version（完整扰动由 seed 精确重放；此处仅存摘要+哈希）",
        }


def _poisson(rng: random.Random, lam: float) -> int:
    """stdlib 泊松抽样（Knuth 累加法），CPython 无 random.poisson。"""
    if lam <= 0.0:
        return 0
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def sample_reality(sc, seed,
                   arrival_sigma: float = 12.0 * SEC_PER_MIN,
                   process_cv: float = 0.08,
                   failure_expected_per_line: float = 1.1,
                   repair_mean_min: float = 40.0) -> Reality:
    """由 seed 生成唯一随机现实；可复现，CRN 公平。
    - 到货延迟：每批 gauss(sigma)。
    - 逐车逐工位耗时乘子：对数正态 exp(cv*Z - cv^2/2)，Z~N(0,1)。
    - 故障：每线 Poisson 故障台数、指数型 MTBF 到达、指数修复时长；可为零。
    """
    rng = random.Random(seed)
    horizon = sc.calendar.horizon

    delays = {}
    for d in sc.deliveries:
        delays[d.batch_id] = rng.gauss(0.0, arrival_sigma)

    # 精确对数正态：实际变异系数即 process_cv
    #   sigma = sqrt(log(1+cv^2))，mu = -sigma^2/2 → E[mul]=1 且 CV(mul)=cv
    sigma_cv = math.sqrt(math.log1p(process_cv * process_cv))
    mu_cv = -0.5 * sigma_cv * sigma_cv
    proc = {}
    for o in sc.orders:
        for st in STAGES:
            if st in sc.configs[o.config].process_sec:
                z = rng.gauss(0.0, 1.0)                      # 标准正态
                proc[(o.vin, st)] = math.exp(mu_cv + sigma_cv * z)

    # 故障：固定区间 [0,H]，N~Pois(lambda)，到达 Uniform(0,H)（条件排序）= 标准泊松过程；
    # 维修指数，末端 clamp H；重叠窗由 _advance in_fault 跳转自然处理。
    failure = {}
    repair_mean_sec = repair_mean_min * SEC_PER_MIN
    for st in STAGES:
        n = _poisson(rng, failure_expected_per_line)           # 允许 0
        starts = sorted(rng.uniform(0.0, horizon) for _ in range(n))
        wl = []
        for t0s in starts:
            dur = rng.expovariate(1.0 / repair_mean_sec)
            end = min(horizon, t0s + dur)                       # clamp at H
            if end > t0s:
                wl.append((t0s, end))
        failure[st] = sorted(wl)

    return Reality(sample_seed=seed, delays=delays, proc_multiplier=proc,
                   failure_windows=failure,
                   params={
                       "arrival_sigma": arrival_sigma,
                       "process_cv": process_cv,
                       "cv_is_actual_cv": True,
                       "failure_expected_per_line": failure_expected_per_line,
                       "repair_mean_min": repair_mean_min,
                       "horizon_sec": horizon,
                       "alg_version": SAMPLE_ALG_VERSION,
                   })


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass
class StratResult:
    strategy: str
    sample_seed: int
    active_orders: int
    completed: int = 0
    on_time: int = 0
    blocked_sec: float = 0.0
    makespan: float = 0.0
    tardy_sum: float = 0.0           # 拖期绝对秒（仅对完成车）
    tardy_cnt: int = 0
    failed: bool = False
    error: Optional[str] = None
    timeline: List[Dict] = field(default_factory=list)

    @property
    def unmet(self):
        return self.active_orders - self.completed

    @property
    def completion_rate(self):
        return (self.completed / self.active_orders) if self.active_orders else 0.0

    @property
    def overall_on_time_rate(self):
        return (self.on_time / self.active_orders) if self.active_orders else 0.0

    @property
    def conditional_on_time_rate(self):
        # completed==0 → None（无完成车时无条件准时率）
        return (self.on_time / self.completed) if self.completed else None

    @property
    def mean_tardy_over_completed_sec(self):
        # 分母=全部已完成（含拖期为0的车）
        return (self.tardy_sum / self.completed) if self.completed else None

    @property
    def mean_tardy_over_late_sec(self):
        # 分母=仅拖期车
        return (self.tardy_sum / self.tardy_cnt) if self.tardy_cnt else None

    def to_dict(self):
        def r(x, nd=5):
            return (round(x, nd) if x is not None else None)
        return {
            "strategy": self.strategy, "sample_seed": self.sample_seed,
            "active_orders": self.active_orders, "completed": self.completed,
            "unmet": self.unmet, "on_time": self.on_time,
            "completion_rate": r(self.completion_rate),
            "overall_on_time_rate": r(self.overall_on_time_rate),
            "conditional_on_time_rate": r(self.conditional_on_time_rate, 5),
            "blocked_sec": r(self.blocked_sec, 2),
            "makespan": r(self.makespan, 2),
            "mean_tardy_over_completed_sec": r(self.mean_tardy_over_completed_sec, 2),
            "mean_tardy_over_late_sec": r(self.mean_tardy_over_late_sec, 2),
            "tardy_cnt": self.tardy_cnt,
            "failed": self.failed, "error": self.error,
        }


# --------------------------------------------------------------------------- #
# DES 核心
# --------------------------------------------------------------------------- #
class BottleneckLine:
    """秒级瓶颈流水 DES：三车间串行、有限缓冲、正阻塞不丢车、自然班历内推进。"""

    def __init__(self, scenario, reality, strategy, w_fixed_seq=None):
        self.sc = scenario
        self.rt = reality
        self.strategy = strategy
        self.cal = scenario.calendar
        self.w_fixed = list(w_fixed_seq) if w_fixed_seq is not None else []
        self.t = 0.0
        self.seq_counter = 0
        self.stock = dict(scenario.initial_stock)
        self.buffers = {"W->P": [], "P->A": []}
        self.machines = {st: {"state": "idle", "vin": None, "start": 0.0,
                              "finish": None, "blocked_since": None,
                              "last_attr": ""} for st in STAGES}
        self.events = []                      # (time, seq, kind, arg)

        # 已开工记录（防重）：(vin, stage)。
        self.started = set()
        # 已开工于 W 的 vin：两个候选堆统一按此集合惰性删除。
        self.w_started = set()
        self.released = set()                 # 已释放订单 vin
        self.start_time = {}                  # (vin, stage) -> start（绝对秒）
        self.finish_time = {}                 # (vin, stage) -> finish（可为 inf：horizon 内未完工）
        self.completed_vins = set()
        self.consumed = {}                    # material -> 累计消耗
        self.delivered = {}                   # material -> 累计到货（已入账部分）
        self.max_buf = {k: 0 for k in self.buffers}
        self.violations = []                  # 物理不变量违规（非空 => 本样本失败）

        self.active = len(scenario.orders)
        self.completed = 0
        self.on_time = 0
        self.blocked_sec = 0.0
        self.makespan = 0.0
        self.tardy_sum = 0.0
        self.tardy_cnt = 0
        self.timeline = []

        self._release_sorted = sorted(scenario.orders, key=lambda e: e.release_at)
        self._rel_idx = 0
        self._ready_heap = []                  # (priority, rel, vin)
        self._w_seq_idx = {v: i for i, v in enumerate(self.w_fixed)} if self.w_fixed else {}
        self._seq_heap = []                    # (seq_idx, rel, vin) 仅 OPTIMIZED
        self._order_map = {o.vin: o for o in scenario.orders}

        # 死循环防护：同刻只安排一个唤醒；事件总数/同刻事件数均设硬上限，
        # 一旦超限直接抛错（样本记失败），而不是静默 0 进度。
        self._pending_wakeups = set()
        self._same_t = None
        self._same_t_n = 0
        self._events_processed = 0
        n_cap = 3 * len(scenario.orders) + len(scenario.deliveries) + 64
        self._event_budget = 8 * n_cap + 10000
        self._push(at=0.0, kind="refresh")

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def _push(self, kind, arg=None, at=None):
        self.seq_counter += 1
        heapq.heappush(self.events, (self.t if at is None else at,
                                     self.seq_counter, kind, arg))

    def _order(self, vin):
        return self._order_map.get(vin)

    def _bom(self, vin, stage):
        return self.sc.configs[self._order(vin).config].bom(stage)

    def _dur(self, vin, stage):
        cfg = self.sc.configs[self._order(vin).config]
        base = cfg.process_sec.get(stage, 60.0)
        sw = 0.0
        prev = self.machines[stage]["last_attr"]
        cur = cfg.color if stage == "P" else cfg.model
        if prev and prev != cur:
            sw = cfg.switch_sec
        mult = self.rt.proc_multiplier.get((vin, stage), 1.0)
        return (base + sw) * mult

    def _violate(self, msg: str):
        """记录物理不变量违规（去重、限量），最终使样本 failed，绝不计为成功。"""
        if len(self.violations) >= 50:
            return
        if msg not in self.violations:
            self.violations.append(msg)

    def _advance(self, vin, stage, start, dur) -> float:
        """按自然班历推进 duration 秒有效工作时间；非工作窗口 + 故障窗均为暂停顺延。

        返回完工绝对秒；若在 horizon 内无法完成则返回 math.inf（该车在 horizon 内未完工）。
        每一步都必须严格推进 cur / 减少 rem，否则立即判定异常并返回 inf（由不变量校验暴露）。
        """
        if dur <= 0.0:
            return start
        rem = dur
        cur = start
        fwin = self.rt.failure_windows.get(stage, [])
        horizon = self.cal.horizon
        max_iter = 8 * (len(fwin) + len(self.cal.windows)) + 64
        for _ in range(max_iter):
            prev_cur = cur
            # 1) 若处于故障窗，先跳到窗末（机器停机）
            b_end = next((b for a, b in fwin if a <= cur < b), None)
            if b_end is not None and b_end > cur:
                cur = b_end
                continue
            # 2) 若处于非工作窗口，跳到下一工作窗
            if not self.cal.in_work(cur):
                nxt = self.cal.next_work_start(cur)
                if nxt <= cur or nxt >= horizon:
                    return math.inf
                cur = nxt
                continue
            we = self.cal.work_end(cur)
            if we is None or we <= cur:
                self._violate(f"班历异常：t={cur} 无有效工作窗末端")
                return math.inf
            nf = [a for a, b in fwin if a > cur and a < we]        # 窗内故障起点
            boundary = we if not nf else min(nf)
            span = boundary - cur
            if span <= 0.0:                                       # 未能推进，避免死循环
                self._violate(f"推进异常：{vin}@{stage} t={cur} boundary={boundary}")
                return math.inf
            if rem <= span + _EPS:
                return min(cur + rem, we)     # 夹到本窗末，保证 fin <= horizon（事件不被丢弃）
            rem -= span
            if rem <= _EPS:                                       # 浮点残差，直接落在边界
                return boundary
            cur = boundary
            if cur <= prev_cur:                                   # 未能推进，避免死循环
                self._violate(f"推进异常：{vin}@{stage} t={cur} 未前进")
                return math.inf
        self._violate(f"推进迭代超限：{vin}@{stage} t={cur} rem={rem}")
        return math.inf

    def _can_work(self, stage):
        return self.cal.in_work(self.t)

    def _has_pending_work(self) -> bool:
        """是否还存在可达工作：待加工订单 / 缓冲在制品 / 非空闲机器。"""
        if len(self.released) > len(self.w_started):
            return True
        if self.buffers["W->P"] or self.buffers["P->A"]:
            return True
        return any(m["state"] != "idle" for m in self.machines.values())

    # ------------------------------------------------------------------ 主循环 -------------
    def run(self, stop_at: Optional[float] = None) -> StratResult:
        stop = self.cal.horizon if stop_at is None else stop_at
        for d in self.sc.deliveries:
            actual = max(0.0, d.planned_at + self.rt.delays.get(d.batch_id, 0.0))
            self._push(kind="delivery", arg=d, at=actual)
        while self.events:
            at, _, kind, arg = heapq.heappop(self.events)
            if at > stop:
                break
            self._events_processed += 1
            if self._events_processed > self._event_budget:
                raise RuntimeError(
                    f"仿真事件数超过预算 {self._event_budget}（疑似事件死循环/时间未推进）："
                    f"t={at!r} kind={kind}")
            if at < self.t - _TIME_EPS:
                raise RuntimeError(f"事件时间回退（逻辑错误）：at={at!r} < t={self.t!r} kind={kind}")
            if self._same_t is not None and abs(at - self._same_t) <= _TIME_EPS:
                self._same_t_n += 1
                if self._same_t_n > _SAME_TIME_EVENT_LIMIT:
                    raise RuntimeError(
                        f"同一时刻事件数超过 {_SAME_TIME_EVENT_LIMIT}（疑似同刻事件无限重复）：t={at!r}")
            else:
                self._same_t = at
                self._same_t_n = 1
            self.t = at
            if kind == "refresh":
                self._pending_wakeups.discard(at)
                self._refresh()
            elif kind == "done":
                stage, vin = arg
                self._on_process_done(stage, vin)
            elif kind == "delivery":
                d = arg
                self.stock[d.material] = self.stock.get(d.material, 0.0) + d.qty
                self.delivered[d.material] = self.delivered.get(d.material, 0.0) + d.qty
                self._refresh()
            else:
                raise ValueError(f"unknown event {kind}")
        self._validate(stop)
        return self._make_result()

    def _refresh(self):
        changed = True
        guard = 0
        while changed:
            guard += 1
            if guard > _REFRESH_LOOP_LIMIT:
                raise RuntimeError(
                    f"_refresh 状态迁移次数超过 {_REFRESH_LOOP_LIMIT}（疑似死循环）：t={self.t!r}")
            changed = False
            changed |= self._release_expired()
            for st in ("W", "P"):
                if self.machines[st]["state"] == "blocked":
                    if self._try_unblock(st):
                        changed = True
            for st in STAGES:
                if self.machines[st]["state"] == "idle":
                    if self._start_stage(st):
                        changed = True
        self._schedule_next_wakeup()

    def _release_expired(self):
        mark = False
        while self._rel_idx < len(self._release_sorted):
            o = self._release_sorted[self._rel_idx]
            if o.release_at > self.t + _TIME_EPS:
                break
            self._rel_idx += 1
            self.released.add(o.vin)
            heapq.heappush(self._ready_heap, (o.priority, o.release_at, o.vin))
            if self.strategy == "OPTIMIZED":
                # 仅 OPTIMIZED 使用固定序列堆；其余策略不必入堆（省内存/时间）
                heapq.heappush(self._seq_heap, (self._w_seq_idx.get(o.vin, 1 << 30),
                                                o.release_at, o.vin))
            mark = True
        return mark

    def _schedule_next_wakeup(self):
        """安排"能改变状态"的唤醒；同一时刻至多一个，且仅当没有更早事件会触发刷新时。

        旧的"每个事件都推一个 refresh"会在同一时刻堆叠大量重复唤醒（同刻 refresh 风暴），
        这里用 _pending_wakeups 去重 + "更早事件优先"条件剪掉冗余唤醒。
        """
        t_next_event = self.events[0][0] if self.events else math.inf
        cands = []
        # 1) 下一个订单释放时刻（释放不是独立事件，必须有唤醒触发）
        if self._rel_idx < len(self._release_sorted):
            cands.append(self._release_sorted[self._rel_idx].release_at)
        # 2) 非工作窗口但仍有在制品/待加工 → 下一工作窗开始时唤醒（否则会漏掉整段产能）
        if not self._can_work("W") and self._has_pending_work():
            nw = self.cal.next_work_start(self.t)
            if self.t < nw < self.cal.horizon:
                cands.append(nw)
        for cand in cands:
            if not (self.t < cand < self.cal.horizon):
                continue
            if cand >= t_next_event - _TIME_EPS:
                continue            # 已有不晚于该时刻的事件，它触发刷新时会重新排程
            if cand in self._pending_wakeups:
                continue
            self._pending_wakeups.add(cand)
            self._push(kind="refresh", at=cand)

    def _try_unblock(self, stage):
        m = self.machines[stage]
        vin = m["vin"]
        if stage == "W":
            buf = self.buffers["W->P"]
            cap = self.sc.buffer_cap["W->P"]
            if len(buf) >= cap:
                return False
            buf.append(vin)
            self._mark_buf("W->P")
        else:  # stage == "P"
            if vin in self.sc.pullout_set:
                self._complete(vin)
                m["state"] = "idle"; m["vin"] = None
                self._end_block(m)
                return True
            buf = self.buffers["P->A"]
            cap = self.sc.buffer_cap["P->A"]
            if len(buf) >= cap:
                return False
            buf.append(vin)
            self._mark_buf("P->A")
        m["state"] = "idle"
        m["vin"] = None
        self._end_block(m)
        return True

    def _mark_buf(self, buf_key):
        n = len(self.buffers[buf_key])
        if n > self.max_buf.get(buf_key, 0):
            self.max_buf[buf_key] = n
        cap = self.sc.buffer_cap.get(buf_key)
        if cap is not None and n > cap:
            self._violate(f"缓冲超上限 {buf_key}: {n} > {cap}")

    def _end_block(self, m):
        if m.get("blocked_since") is not None:
            self.blocked_sec += self.t - m["blocked_since"]
            m["blocked_since"] = None

    def _heap_peek(self, heap, started):
        """取堆顶候选；已开工（started）条目惰性删除，避免重复开工与堆不一致。"""
        while heap and heap[0][2] in started:
            heapq.heappop(heap)
        return heap[0][2] if heap else None

    def _start_stage(self, stage):
        if not self._can_work(stage):
            return False
        vin = self._choose(stage)
        if vin is None:
            return False
        if (vin, stage) in self.started:
            self._violate(f"重复开工 {vin}@{stage}")
            return False
        # 1) 物料校验：不通过则既不取队列也不消耗物料（选择阶段绝不改动队列）
        bom = self._bom(vin, stage)
        for mat, qty in bom.items():
            if self.stock.get(mat, 0.0) < qty:
                return False                     # 待料等来货（不推空转，靠 delivery / 释放唤醒）
        # 2) 校验通过才真正出队（W：惰性标记；P/A：从对应缓冲移除）
        if stage == "W":
            self.w_started.add(vin)
        else:
            buf_key = "W->P" if stage == "P" else "P->A"
            try:
                self.buffers[buf_key].remove(vin)
            except ValueError:
                self._violate(f"缓冲取件失败 {buf_key}: {vin}")
                return False
        # 3) 消耗物料并记账
        for mat, qty in bom.items():
            self.stock[mat] = self.stock.get(mat, 0.0) - qty
            if self.stock[mat] < -_STOCK_EPS:
                self._violate(f"库存为负：{mat}={self.stock[mat]}")
            self.consumed[mat] = self.consumed.get(mat, 0.0) + qty
        start = self.t
        dur = self._dur(vin, stage)              # 先算时长（依赖旧 last_attr）…
        fin = self._advance(vin, stage, start, dur)
        o = self._order(vin)
        m = self.machines[stage]
        m["last_attr"] = o.color if stage == "P" else o.model   # …再更新换型/换色基准（W/P/A 全覆盖）
        m["state"] = "proc"; m["vin"] = vin; m["start"] = start; m["finish"] = fin
        self.started.add((vin, stage))
        self.start_time[(vin, stage)] = start
        self.finish_time[(vin, stage)] = fin
        # horizon 内未完工：finish 用 None 展示（JSON 不允许 Infinity），事件仍保留 inf
        self.timeline.append({"vin": vin, "stage": stage,
                              "start": round(start, 2),
                              "finish": (round(fin, 2) if math.isfinite(fin) else None),
                              "finished": bool(math.isfinite(fin))})
        self._push(kind="done", arg=(stage, vin), at=fin)
        return True

    def _choose(self, stage):
        if stage == "W":
            return self._choose_w()
        if stage == "P":
            return self._choose_buf("W->P", self.machines["P"], by_color=True)
        return self._choose_buf("P->A", self.machines["A"], by_color=False)

    def _choose_w(self):
        # 选择阶段只读不写：候选与队列改动解耦（取队列在 _start_stage 校验物料通过后）
        if self.strategy == "OPTIMIZED":
            vin = self._heap_peek(self._seq_heap, self.w_started)
            if vin is not None:
                return vin
        return self._heap_peek(self._ready_heap, self.w_started)

    def _choose_buf(self, buf_key, m, by_color):
        """按策略挑缓冲内最优车辆；只读，不移除（移除由 _start_stage 在校验通过后执行）。"""
        buf = self.buffers[buf_key]
        if not buf:
            return None
        stage = "P" if buf_key == "W->P" else "A"
        last = m["last_attr"]
        best_key = None
        best_vin = None
        for vin in buf:
            if (vin, stage) in self.started:
                self._violate(f"缓冲 {buf_key} 含已开工车辆 {vin}@{stage}")
                continue
            o = self._order(vin)
            if o is None:
                self._violate(f"缓冲 {buf_key} 含未知车辆 {vin}")
                continue
            if self.strategy == "OPTIMIZED" and self._w_seq_idx:
                # 综合：全阶段按固定序列 Index 优先执行（便于公平比较固定方案）
                key = (self._w_seq_idx.get(vin, 1 << 30), o.priority, o.due_at, vin)
            else:
                if self.strategy == "COLOR_GROUP" and by_color:
                    same = 0 if (last and o.color == last) else 1
                elif self.strategy == "LOAD_BALANCE" and not by_color:
                    same = 0 if (last and o.model == last) else 1
                else:
                    same = 0
                key = (same, o.priority, o.due_at, vin)
            if best_key is None or key < best_key:
                best_key = key
                best_vin = vin
        return best_vin

    def _on_process_done(self, stage, vin):
        m = self.machines[stage]
        if m["vin"] != vin:
            self._violate(f"完工事件与机器状态不一致：{stage} 机器={m['vin']} 事件={vin}")
        m["state"] = "idle"; m["vin"] = None
        if stage == "A" or (stage == "P" and vin in self.sc.pullout_set):
            self._complete(vin)
            self._refresh()
            return
        nxt = "W->P" if stage == "W" else "P->A"
        buf = self.buffers[nxt]
        cap = self.sc.buffer_cap[nxt]
        if len(buf) >= cap:
            m["state"] = "blocked"; m["vin"] = vin; m["blocked_since"] = self.t
        else:
            buf.append(vin)
            self._mark_buf(nxt)
        self._refresh()

    def _complete(self, vin):
        if vin in self.completed_vins:
            self._violate(f"重复完成 {vin}")
            return
        if (vin, "A") not in self.finish_time and not (
                (vin, "P") in self.finish_time and vin in self.sc.pullout_set):
            self._violate(f"{vin} 未走完末道工序即被计为完成")
        self.completed += 1
        o = self._order(vin)
        if self.t > self.makespan:
            self.makespan = self.t
        if self.t <= o.due_at:
            self.on_time += 1
        else:
            self.tardy_cnt += 1
            self.tardy_sum += self.t - o.due_at
        self.completed_vins.add(vin)

    # ------------------------------------------------------------------ #
    # 物理不变量独立校验（与事件循环解耦，样本失败不得报成功）
    # ------------------------------------------------------------------ #
    def _validate(self, stop):
        # 1) 唯一开工
        if len(self.started) != len(self.timeline):
            self._violate(f"timeline 条数({len(self.timeline)}) != 唯一开工集合({len(self.started)})")
        for k in self.start_time:
            if k not in self.started:
                self._violate(f"{k[0]}@{k[1]} 有开工时刻但未登记开工")
        # 2) 开工时刻在工作窗口内 / 完工不早于开工
        for (vin, stage), t0 in self.start_time.items():
            if not self.cal.in_work(t0):
                self._violate(f"{vin}@{stage} 在非工作窗口开工 t={t0}")
            fin = self.finish_time.get((vin, stage))
            if fin is not None and math.isfinite(fin) and fin < t0 - _TIME_EPS:
                self._violate(f"{vin}@{stage} 完工({fin})早于开工({t0})")
        # 3) 工序先后：后道开工不得早于前道完工
        for (vin, stage), t0 in self.start_time.items():
            i = STAGES.index(stage)
            if i == 0:
                continue
            prev = STAGES[i - 1]
            pf = self.finish_time.get((vin, prev))
            if pf is None:
                self._violate(f"{vin} 未在{prev}完工即开工{stage}")
            elif not math.isfinite(pf):
                self._violate(f"{vin} 在{prev}未完工(inf)即开工{stage}")
            elif t0 < pf - _TIME_EPS:
                self._violate(f"{vin} {stage}开工({t0})早于{prev}完工({pf})")
        # 4) 完成口径与会计一致
        if self.completed != len(self.completed_vins):
            self._violate(f"完成计数({self.completed}) != 完成集合({len(self.completed_vins)})")
        if self.completed > self.active:
            self._violate(f"完成数({self.completed}) > 投放数({self.active})")
        if self.on_time + self.tardy_cnt != self.completed:
            self._violate(f"准时({self.on_time})+拖期({self.tardy_cnt}) != 完成({self.completed})")
        if self.blocked_sec < -_TIME_EPS:
            self._violate(f"阻塞时长异常：{self.blocked_sec}")
        # 5) 库存非负 + 物料台账守恒（期初 + 到货 - 消耗 = 结存）
        for mat, q in self.stock.items():
            if q < -_STOCK_EPS:
                self._violate(f"库存为负：{mat}={q}")
        for mat, q0 in self.sc.initial_stock.items():
            exp = q0 + self.delivered.get(mat, 0.0) - self.consumed.get(mat, 0.0)
            tol = 1e-6 * max(1.0, abs(exp))
            if abs(exp - self.stock.get(mat, 0.0)) > tol:
                self._violate(f"物料台账不符 {mat}: 期初+到货-消耗={exp} != 结存={self.stock.get(mat, 0.0)}")
        # 6) 缓冲上限
        for k, cap in self.sc.buffer_cap.items():
            if self.max_buf.get(k, 0) > cap:
                self._violate(f"缓冲曾超上限 {k}: {self.max_buf.get(k)} > {cap}")
        # 7) 不丢车：完整推进到 horizon 时，完工车辆必须可追溯
        if stop >= self.cal.horizon - _TIME_EPS:
            self._validate_flow()

    def _validate_flow(self):
        """W→P、P→A 流守恒：前道完工车必须处于"缓冲 / 阻塞持有 / 后道已开工"之一。"""
        for up, down in (("W", "P"), ("P", "A")):
            buf_key = f"{up}->{down}"
            buf = set(self.buffers[buf_key])
            nxt_started = {vin for (vin, st) in self.started if st == down}
            hold = set()
            mu = self.machines[up]
            if mu["state"] == "blocked" and mu["vin"] is not None:
                hold.add(mu["vin"])
            lost = 0
            for (vin, st) in self.started:
                if st != up:
                    continue
                fin = self.finish_time.get((vin, up))
                if fin is None or not math.isfinite(fin):
                    continue
                if vin in self.sc.pullout_set and up == "P":
                    continue
                if vin in buf or vin in nxt_started or vin in hold:
                    continue
                lost += 1
                if lost <= 5:
                    self._violate(f"{vin} 在 {up} 完工后丢失（不在 {buf_key} 缓冲/阻塞位/{down} 已开工）")
            if lost:
                self._violate(f"{up}->{down} 丢失 {lost} 台（缓冲/阻塞/后续工序均查不到）")

    def _make_result(self):
        err = None
        if self.violations:
            head = "; ".join(self.violations[:8])
            more = f" …(+{len(self.violations) - 8})" if len(self.violations) > 8 else ""
            err = "物理不变量校验失败：" + head + more
        return StratResult(strategy=self.strategy, sample_seed=self.rt.sample_seed,
                           active_orders=self.active, completed=self.completed,
                           on_time=self.on_time, blocked_sec=self.blocked_sec,
                           makespan=self.makespan, tardy_sum=self.tardy_sum,
                           tardy_cnt=self.tardy_cnt, timeline=self.timeline,
                           failed=bool(self.violations), error=err)


def simulate(scenario, reality, strategy, w_fixed_seq=None):
    sim = BottleneckLine(scenario, reality, strategy, w_fixed_seq=w_fixed_seq)
    return sim.run()


# --------------------------------------------------------------------------- #
# 名义阶段优化（确定性，非 ALNS；不读随机未来）→ 固定序列
# --------------------------------------------------------------------------- #
def _nominal_reality(sc) -> Reality:
    """无扰动名义现实：proc=1、无到货延迟、无故障。供名义选优与（fake）SNR 用。"""
    proc = {(o.vin, st): 1.0 for o in sc.orders for st in STAGES}
    delays = {d.batch_id: 0.0 for d in sc.deliveries}
    return Reality(sample_seed=0, delays=delays, proc_multiplier=proc,
                   failure_windows={st: [] for st in STAGES},
                   params={"nominal": True, "alg_version": SAMPLE_ALG_VERSION})


def _nominal_score(res: StratResult) -> Tuple:
    """名义评价打分：优先 完成率→整体按期→条件按期→阻塞(越少越好)→makespan(越小越好)。
    返回越大越好的元组。"""
    return (res.completion_rate,
            res.overall_on_time_rate,
            res.conditional_on_time_rate or 0.0,
            -res.blocked_sec,
            -res.makespan)


def optimize_nominal_plan(sc, seed: int = 0, n_iter: int = 3,
                         should_cancel: Optional[Callable[[], bool]] = None) -> List[str]:
    """名义（无扰动）候选选优：生成若干确定性候选序列、在名义现实上仿真、选出最优固定序列。

    - 关键点：不调用随机采样，不读每样本未来；同一固定序列供全部样本复用、全阶段执行。
    - 候选：按 优先级 / 交期 EDD / 颜色分组 / 车型分组 的确定性排序 + 确定性 2-opt 微扰。
    - 用 BottleneckLine + strategy="OPTIMIZED" 对每个候选在名义现实上做仿真评估。
    - should_cancel 在每个候选之间检查一次，避免大场景长时间占用时无法取消。
    """
    orders = sc.orders

    def by_due():
        return sorted(orders, key=lambda o: (o.due_at, o.priority, o.release_at, o.vin))

    def by_prio():
        return sorted(orders, key=lambda o: (o.priority, o.due_at, o.release_at, o.vin))

    def by_color():
        return sorted(orders, key=lambda o: (o.color, o.priority, o.due_at, o.vin))

    def by_model():
        return sorted(orders, key=lambda o: (o.model, o.priority, o.due_at, o.vin))

    candidates = []
    for fn in (by_due, by_prio, by_color, by_model):
        candidates.append([o.vin for o in fn()])

    # 确定性 2-opt 微扰若干次，丰富候选（无 RNG 用 hash 确定性）
    base = candidates[0][:]
    r = random.Random(seed)
    for _ in range(n_iter * 2):
        c = base[:]
        i = r.randrange(len(c)); j = r.randrange(len(c))
        c[i], c[j] = c[j], c[i]
        candidates.append(c)

    nominal = _nominal_reality(sc)
    best_seq, best_score = None, None
    for seq in candidates:
        if should_cancel is not None and should_cancel():
            break
        try:
            res = simulate(sc, nominal, "OPTIMIZED", w_fixed_seq=seq)
            # 违反物理不变量的候选不得被选为名义方案
            s = _nominal_score(res) if not res.failed else (-1e9,)
        except Exception:
            s = (-1e9,)
        if best_score is None or s > best_score:
            best_score = s
            best_seq = seq
    return best_seq or [o.vin for o in by_due()]


# --------------------------------------------------------------------------- #
# MC 配置
# --------------------------------------------------------------------------- #
@dataclass
class MCConfig:
    n_samples: int = 1000
    seed_base: int = 0
    strategies: List[str] = field(default_factory=lambda: list(STRATEGIES))
    arrival_sigma: float = 12.0 * SEC_PER_MIN
    process_cv: float = 0.08
    failure_expected_per_line: float = 1.1
    repair_mean_min: float = 40.0


def run_samples(sc, cfg: MCConfig,
                progress: Optional[Callable[[int, int], None]] = None,
                on_sample: Optional[Callable[[int, int, object, object], None]] = None,
                should_cancel: Optional[Callable[[], bool]] = None,
                keep_sample0_timeline: bool = True) -> Dict:
    """按样本执行各策略。

    事件回调：
      - progress(done, total)：每样本结束后走一次。
      - on_sample(idx, n_total, sample_record, timelines_or_none)：每样本流式回调（给集成暂态展示）。
        idx 为 0-based 样本序号；timelines_or_none 仅 keep_sample0_timeline 且 idx==0 时非 None。
      - should_cancel() 若返回 True 则提前停（返回 cancelled=True, n_completed）。

    返回 Dict（见 run_samples 顶部注释 / 文档），含 cancelled / n_completed / running / strategy_stats。
    失败样本（异常或物理不变量违规）不计入统计累计，且写入 errors，final_status 必为 not_complete。
    """
    wfix = optimize_nominal_plan(sc, should_cancel=should_cancel)

    # 每策略累计器
    acc = {st: {"completion": [], "overall": [], "conditional": [],
                "blocked": [], "makespan": [], "tardy_cov": [], "tardy_late": []}
           for st in cfg.strategies}

    errors = []
    sample_rows = []
    sample0_timelines = {}
    cancelled = False
    n_completed = 0

    for s in range(cfg.n_samples):
        if should_cancel and should_cancel():
            cancelled = True
            break
        seed = cfg.seed_base + s
        try:
            rt_reality = sample_reality(sc, seed, cfg.arrival_sigma, cfg.process_cv,
                                        cfg.failure_expected_per_line,
                                        cfg.repair_mean_min)
        except Exception as e:
            errors.append({"sample_idx": s, "seed": seed, "error": repr(e)})
            if progress:
                progress(s + 1, cfg.n_samples)
            continue

        per_strat = {}
        timelines = {}
        aborted = False
        for st in cfg.strategies:
            if should_cancel and should_cancel():
                cancelled = True
                aborted = True
                break
            try:
                sim = BottleneckLine(sc, rt_reality, st, w_fixed_seq=wfix)
                res = sim.run()
                res.strategy = st
                per_strat[st] = res
                if keep_sample0_timeline and s == 0:
                    timelines[st] = sim.timeline
            except Exception as e:
                errors.append({"sample_idx": s, "seed": seed, "strategy": st,
                               "error": repr(e)})
                per_strat[st] = None
        if aborted:
            break                     # 本样本未跑完：不写样本记录、不计入完成样本数
        if keep_sample0_timeline and s == 0:
            sample0_timelines = timelines

        for st, r in per_strat.items():
            if r is None:
                continue
            if r.failed:
                # 物理不变量违规 → 计为错误样本，绝不进入统计成功口径
                errors.append({"sample_idx": s, "seed": seed, "strategy": st,
                               "error": r.error or "物理不变量校验失败"})
                continue
            acc[st]["completion"].append(r.completion_rate)
            acc[st]["overall"].append(r.overall_on_time_rate)
            acc[st]["conditional"].append(r.conditional_on_time_rate)
            acc[st]["blocked"].append(r.blocked_sec)
            acc[st]["makespan"].append(r.makespan)
            acc[st]["tardy_cov"].append(r.mean_tardy_over_completed_sec)
            acc[st]["tardy_late"].append(r.mean_tardy_over_late_sec)

        record = {
            "sample_idx": s, "seed": seed,
            "reality": rt_reality.to_dict(),
            "strategies": {k: (v.to_dict() if v else None) for k, v in per_strat.items()},
        }
        sample_rows.append(record)
        n_completed += 1

        if on_sample:
            on_sample(s, cfg.n_samples, record,
                      (sample0_timelines if (keep_sample0_timeline and s == 0) else None))
        if progress:
            progress(n_completed, cfg.n_samples)

    # ---- 累计暂态（running） ----
    running = _running_summary(acc)

    n_success = {s: len(acc[s]["completion"]) for s in cfg.strategies}
    complete_ok = (not errors) and all(n_success[s] == n_completed for s in cfg.strategies)
    stats = _aggregate_stats(acc, cfg.strategies)

    return {
        "status_term": ("cancelled" if cancelled else ("complete" if complete_ok else "not_complete")),
        "cancelled": cancelled,
        "n_completed": n_completed,
        "n_samples": cfg.n_samples,
        "samples_success": n_success,
        "errors": errors,
        "running": running,
        "strategy_stats": stats,
        "sample_results": sample_rows,
        "sample0_timeline": sample0_timelines if sample0_timelines else None,
    }


# ---------------- 统计工具 ---------------- #
def _running_summary(acc):
    """返回每策略累计暂态均值（用于集成实时展示），不过度分配。"""
    out = {}
    for st, lst in acc.items():
        out[st] = {}
        for k, vals in lst.items():
            clean = [v for v in vals if v is not None]
            n = len(clean)
            if not clean:
                out[st][k] = {"n": 0, "mean": None}
                continue
            mean = sum(clean) / n
            out[st][k] = {"n": n, "mean": round(mean, 5)}
    return out


def _stats(vals, n_total):
    clean = [v for v in vals if v is not None]
    n_valid = len(clean)
    if not clean:
        return {"n_total": n_total, "n_valid": 0, "mean": None, "std": None,
                "p05": None, "p95": None, "ci95_low": None, "ci95_high": None}
    n = n_valid
    mean = sum(clean) / n
    var = sum((x - mean) ** 2 for x in clean) / n
    sd = math.sqrt(var)
    ss = sorted(clean)
    p05 = ss[max(0, int(0.05 * n) - 1)]
    p95 = ss[min(n - 1, int(0.95 * n) - 1)]
    se = sd / math.sqrt(n)
    return {"n_total": n_total, "n_valid": n,
            "mean": round(mean, 5), "std": round(sd, 5),
            "p05": round(p05, 5), "p95": round(p95, 5),
            "ci95_low": round(mean - 1.96 * se, 5),
            "ci95_high": round(mean + 1.96 * se, 5)}


def _aggregate_stats(acc, strategies):
    out = {}
    for st in strategies:
        n_total = len(acc[st]["completion"])
        out[st] = {
            "completion_rate": _stats(acc[st]["completion"], n_total),
            "overall_on_time": _stats(acc[st]["overall"], n_total),
            "conditional_on_time": _stats(acc[st]["conditional"], n_total),
            "blocked_sec": _stats(acc[st]["blocked"], n_total),
            "makespan": _stats(acc[st]["makespan"], n_total),
            "mean_tardy_over_completed_sec": _stats(acc[st]["tardy_cov"], n_total),
            "mean_tardy_over_late_sec": _stats(acc[st]["tardy_late"], n_total),
        }
    return out


# --------------------------------------------------------------------------- #
# 场景构造（真实自然日历 + BOM）
# --------------------------------------------------------------------------- #
def build_production_scenario(n_vehicles: int = 4200, seed: int = 0,
                              pullout_frac: float = 0.05) -> ProductionScenario:
    """构造周产级平衡场景（默认 4200 台）：
    6 配置、2 缓冲、STEEL/ALUM/BATTERY/MOTOR/PAINT 物料，自然日历 5 日×2 窗×460min。
    释放/交期均放在工作日历窗口内。"""
    rng = random.Random(seed)
    styles = [
        dict(config_id="SUV_WHITE", model="SUV", color="WHITE", raw="STEEL", sw=45.0),
        dict(config_id="SUV_BLACK", model="SUV", color="BLACK", raw="STEEL", sw=45.0),
        dict(config_id="SEDAN_RED", model="SEDAN", color="RED", raw="ALUM", sw=75.0),
        dict(config_id="SEDAN_SILVER", model="SEDAN", color="SILVER", raw="ALUM", sw=65.0),
        dict(config_id="MPV_DARK", model="MPV", color="DARK", raw="STEEL", sw=70.0),
        dict(config_id="PICKUP_BLUE", model="PICKUP", color="BLUE", raw="ALUM", sw=95.0, pullout=True),
    ]
    cfgs: Dict[str, ConfigDef] = {}
    for sty in styles:
        cfgs[sty["config_id"]] = ConfigDef(
            config_id=sty["config_id"], model=sty["model"], color=sty["color"],
            raw=sty["raw"], switch_sec=sty["sw"],
            is_pullout=bool(sty.get("pullout", False)),
            process_sec={"W": 62.0, "P": 58.0, "A": 60.0})

    cal = WorkCalendar()
    windows = cal.windows

    orders: List[Order] = []
    ids = list(cfgs)
    nw = len(windows)                       # 10 个工作窗
    for i in range(n_vehicles):
        cid = ids[i % len(ids)]
        c = cfgs[cid]
        slot = i % nw
        ws, we = windows[slot]
        off = ((i // nw) % int(SHIFT_MIN)) * SEC_PER_MIN      # 窗内分散
        rel = min(ws + off, we - 1)
        due = rel + 1.8 * SHIFT_SEC
        due = due if due <= cal.horizon else cal.horizon
        prio = 3 if rng.random() < 0.5 else (2 if rng.random() < 0.75 else 1)
        orders.append(Order(vin=f"V{i:05d}", config=cid, release_at=rel,
                            due_at=due, priority=prio, model=c.model, color=c.color))

    # 到货：5 个物料分批分布在 horizon 内
    materials = ("STEEL", "ALUM", "PAINT", "BATTERY", "MOTOR")
    deliveries: List[SupplyDelivery] = []
    n_batches = 6
    for b in range(n_batches):
        t = (b / n_batches) * cal.horizon
        for mat in materials:
            deliveries.append(SupplyDelivery(batch_id=f"{mat}_{b}", material=mat,
                                             qty=n_vehicles * 0.9 / n_batches, planned_at=t))
    init_stock = {
        "STEEL": n_vehicles * 0.15, "ALUM": n_vehicles * 0.15,
        "PAINT": n_vehicles * 0.2,
        "BATTERY": n_vehicles * 0.2, "MOTOR": n_vehicles * 0.2,
    }
    pullout_vins = [o.vin for o in orders if cfgs[o.config].is_pullout]
    return ProductionScenario(scenario_id=f"PROD_WEEKLY_{n_vehicles}",
                              orders=orders, configs=cfgs, deliveries=deliveries,
                              initial_stock=init_stock,
                              buffer_cap={"W->P": 40, "P->A": 40},
                              pullout_vins=pullout_vins, calendar=cal)


def main(n_vehicles=4200, n_samples=6, seed=0):
    import json
    sc = build_production_scenario(n_vehicles, seed=seed)
    cfg = MCConfig(n_samples=n_samples, seed_base=seed)
    out = run_samples(sc, cfg)
    short = {k: v for k, v in out.items() if k != "sample_results"}
    print(json.dumps(short, indent=2, default=str))


if __name__ == "__main__":
    import sys
    nv = int(sys.argv[1]) if len(sys.argv) > 1 else 4200
    ns = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    sd = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    main(n_vehicles=nv, n_samples=ns, seed=sd)