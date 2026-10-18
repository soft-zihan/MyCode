"""两次运行放在一起比。

统计部分（配对、McNemar 精确检验、分层）是从 `eval/harbor/compare.py` 搬上来的：
那份已经在 25 题真数据上跑过、并且被 README §5 引用，不重写。

**变量守卫是这个模块存在的理由。** 首轮 harbor 得出"MyCode 快 1.86×"，而两臂的
思考开关根本没对齐（MyCode 发了 `enable_thinking: false`，opencode 没发）；
对齐之后差距缩到 1.17×。假结论能一路活到汇报环节，是因为没有任何一处机制
在对比时问过一句"这两轮的控制变量一样吗"。所以这里：
变量不一致 → `variable_mismatches` 非空 → 渲染时顶部打差异表，
并且**不给任何"谁更强"的措辞**，只给描述性数字。

还有一条同源的坑：**没做校验**时 `variable_mismatches` 也是空的。
"什么都没校验"和"校验通过"在数据上长得一模一样，而 1.86× 那次正是前者。
所以 `compare_reports` 额外带出 `variables_seen`（每侧看到几个控制变量），
而 `_guard_was_blind` 把这些都判成"瞎"：`variables_seen` 键根本不存在
（调用方走的是 `compare_rows`，那一层只看得到行、看不到报告，所以**无从**校验）、
或者任一侧记到的变量数为 0（一侧没记就等于没法比对）。
瞎了就必须把"未经过变量校验"说出来——沉默的守卫等于没有守卫。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from math import comb
from pathlib import Path
from typing import Any

from eval.common.runner_base import REPORTS_DIR
from eval.common.schema import PASSED_THRESHOLD, normalize_row

DEFAULT_METRICS = (
    "duration_s",
    "tokens.input",
    "tokens.output",
    "tokens.reasoning",
    "tokens.cached",
)

# 守卫没做有效校验时打在 headline / markdown 开头的警告。**措辞必须层级无关**：
# `compare_rows` 那一层只看得到逐题行、看不到报告，说"两份报告都没有记录控制变量"
# 在那一层是假话，而一句在某个调用层为假的话不该被继续复用。
#
# 禁词表（"更强/胜出/…"那一类）**不在这里存第二份**：它由
# `tests/unit/test_eval_compare.py` 的 `SUPERLATIVES` / `WINNER_PHRASES` 逐词断言，
# 外加两个守卫分支的金标准串——那份是**真的会被执行**的清单。这里再抄一份就一定会
# 各自漂移（这个仓库已经被咬过两次：loca 的控制变量清单、`eval/harbor/compare.py`
# 至今影子着 `schema.py` 的 PASSED_THRESHOLD）。而且这里存一份是**死的**：
# 生产代码没有任何路径读它，往元组里加词什么效果都没有。
GUARD_BLIND_WARNING = "⚠️ 本次对比未经过变量校验"

# 校验层确实拿到了报告、但某一侧记到的变量数为 0 时，追加在警告后面的原因说明。
# "没有记录控制变量"这句是既有测试与 README §5.7 认的口径，改它就得连测试一起改。
GUARD_NO_VARIABLES_NOTE = "没有记录控制变量"

# 这几个控制变量在报告里有两处写法：`meta.X` 和 `meta.variables.X`（harbor 的
# `run_variables()` 把 model/seed/git_revision 一并塞进 variables，盘上真报告
# 就是这个形状）。比的时候合成一个"有效值"，见 variable_mismatches。
_META_BACKED_VARIABLES = ("model", "seed", "git_revision")

Dimension = Callable[[str, Mapping[str, Any] | None], str | None]


@dataclass(frozen=True)
class VariableMismatch:
    field: str
    a: Any
    b: Any


def load_run(run_id_or_path: str | Path, reports_dir: Path | None = None) -> dict[str, Any]:
    """按路径或 run_id 读一份报告。run_id 就是文件名主干（见 Task 8 的裁定）。"""
    candidate = Path(run_id_or_path)
    if not candidate.suffix:
        candidate = Path(reports_dir or REPORTS_DIR) / f"{candidate}.json"
    if not candidate.exists():
        raise FileNotFoundError(
            f"找不到运行记录 {run_id_or_path!r}（试过 {candidate}）；"
            f"先跑对应的 runner，或用 /api/eval/jobs 看有哪些 run_id"
        )
    return json.loads(candidate.read_text(encoding="utf-8"))


def rows_of(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """取逐题行，统一成标准核心。

    新报告是 `results`（已归一化）；2026-10-18 之前落盘的 harbor 报告是 `trials`。
    两种都要认——那些报告重跑一次要 45 分钟网关时间，而 README §5 的结论引自它们。
    """
    results = report.get("results")
    if results is not None:
        return [dict(r) for r in results]
    benchmark = report.get("benchmark") or "harbor"
    return [normalize_row(r, benchmark=benchmark) for r in report.get("trials") or []]


def _variables_of(report: Mapping[str, Any]) -> dict[str, Any]:
    """取这轮的控制变量。

    两个地方都要看：新报告写在 `meta.variables`（Task 7/8/9 的口径），
    但**盘上已有的 harbor 报告写在 `summary.variables`**——`write_report` 一直是
    `summary["variables"] = dict(variables or {})`（`eval/harbor/runner.py:845`），
    Task 8 之后才是两处都有。只读 `meta` 的话，README §5.7 那个基线对比
    （正是"竞品对比要控好变量"这条要求的原始场景）会拿到两个空字典，
    守卫于是什么都不报——而"什么都没校验"和"校验通过"在输出上长得一模一样。
    这就是上一轮假结论活到汇报环节的机制，不能再来一次。
    """
    meta = report.get("meta") or {}
    variables = meta.get("variables")
    if isinstance(variables, Mapping) and variables:
        return dict(variables)
    summary = report.get("summary") or {}
    fallback = summary.get("variables")
    return dict(fallback) if isinstance(fallback, Mapping) else {}


def _effective_variable(
    meta: Mapping[str, Any], variables: Mapping[str, Any], key: str
) -> Any:
    """`meta.X` 与 `variables.X` 取有值的那一处，合成这个控制变量的有效值。"""
    value = meta.get(key)
    return value if value is not None else variables.get(key)


def variable_mismatches(
    a: Mapping[str, Any], b: Mapping[str, Any]
) -> list[VariableMismatch]:
    """逐项比控制变量。空列表 = 对齐（但**不一定**代表校验过，见 `_guard_was_blind`）。

    比的是：benchmark、meta.model、meta.seed、meta.git_revision、meta.task_ids，
    以及 `_variables_of(...)` 两边键的**并集**（`_variables_of` 会同时看
    `meta.variables` 和旧 harbor 报告的 `summary.variables`）。并集是必须的：
    只比交集的话，一份没记录某个变量的旧报告当基线，就等于默认那个变量对齐了。

    model/seed/git_revision 三项按**有效值**比而不是两处各比一次：这两处在同一份
    报告里来自同一个入参，一处不同必然两处都不同，分开报会把一项差异刷成两行噪音。
    合成有效值而不是"只读 meta"是必须的——盘上的旧 harbor 报告根本没有 `meta`，
    变量只在 `summary.variables` 里，只读 meta 会让 model 差异整个漏掉。
    """
    found: list[VariableMismatch] = []

    def check(field: str, left: Any, right: Any) -> None:
        if left != right:
            found.append(VariableMismatch(field, left, right))

    check("benchmark", a.get("benchmark"), b.get("benchmark"))
    meta_a, meta_b = a.get("meta") or {}, b.get("meta") or {}
    vars_a, vars_b = _variables_of(a), _variables_of(b)
    for key in _META_BACKED_VARIABLES:
        check(
            f"meta.{key}",
            _effective_variable(meta_a, vars_a, key),
            _effective_variable(meta_b, vars_b, key),
        )

    ids_a = set(meta_a.get("task_ids") or [r.get("task_id") for r in rows_of(a)])
    ids_b = set(meta_b.get("task_ids") or [r.get("task_id") for r in rows_of(b)])
    if ids_a != ids_b:
        check(
            "meta.task_ids",
            sorted(ids_a - ids_b) or f"{len(ids_a)} 题",
            sorted(ids_b - ids_a) or f"{len(ids_b)} 题",
        )

    for key in sorted(set(vars_a) | set(vars_b)):
        if key in _META_BACKED_VARIABLES:
            continue  # 上面已按有效值比过，再比一次只是重复噪音
        check(f"variables.{key}", vars_a.get(key), vars_b.get(key))
    return found


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """McNemar 精确检验的双侧 p 值。

    只有"两侧判定不一致"的题携带信息。两侧都过或都没过的题不进入检验，
    所以 25 题里可能只有几题进来，p 值自然大——那是样本量的事实，不是没差别。
    """
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2**n
    return min(1.0, 2 * tail)


def _at(row: Mapping[str, Any] | None, path: str) -> Any:
    cur: Any = row
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


def _passed(row: Mapping[str, Any] | None) -> bool | None:
    """判分口径唯一：`passed`。None = 没判分，不能当成失败。

    `passed` 这个键**在**、值是 None，就是"这一题没判分"（schema 的口径），
    必须留在配对分母之外；键**根本不在**才是旧形状的行（harbor 原始 trial 只有
    reward），这时按 PASSED_THRESHOLD 从 reward 推一次。两种情况必须分开：
    合成一句"值为 None 就回落到 reward"，会把显式未判分的行读成"判了没过"，
    悄悄塞进分母、压低通过率——正是本模块要防的那类假数字。
    """
    if not isinstance(row, Mapping):
        return None
    if "passed" in row:
        value = row.get("passed")
        return None if value is None else bool(value)
    reward = _at(row, "reward")
    return None if reward is None else bool(reward >= PASSED_THRESHOLD)


def pair_rows(
    rows_a: Sequence[Mapping[str, Any]],
    rows_b: Sequence[Mapping[str, Any]],
    *,
    dimension: Dimension | None = None,
) -> list[dict[str, Any]]:
    """按 task_id 配对。只有一侧跑过的题保留在 pairs 里，但不进配对统计。

    同名多行（多 attempt）留最后一个，也就是最新那次。
    剔除（`exclude`）不在这里做：`compare_rows` 要先看到全集才能报出"剔了哪些"。
    """
    left = {r["task_id"]: r for r in rows_a if r.get("task_id")}
    right = {r["task_id"]: r for r in rows_b if r.get("task_id")}
    pairs: list[dict[str, Any]] = []
    for task_id in sorted(set(left) | set(right)):
        row_a, row_b = left.get(task_id), right.get(task_id)
        pairs.append({
            "task_id": task_id,
            "dimension": dimension(task_id, row_a or row_b) if dimension else None,
            "a_passed": _passed(row_a),
            "b_passed": _passed(row_b),
            "a": row_a,
            "b": row_b,
        })
    return pairs


def _mean(values: Sequence[Any]) -> float | None:
    """均值。`None`（以及任何非数值）一律**不进分母**——未知不是 0。"""
    known = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(known) / len(known), 1) if known else None


def _metric_value(row: Mapping[str, Any] | None, path: str) -> Any:
    """取一个指标值，并把 `duration_s` 的 0.0 哨兵还原成"未知"。

    `normalize_row` 把未知的耗时兜成 `0.0`（`schema.py:177`），而 `summarize_rows`
    用 `duration_s > 0` 当"真有时长"（`schema.py:189-190`）。对比层必须用同一条口径：
    否则"根本没跑起来"的题会以 0 秒进均值，把平均耗时拉低——而没跑起来的题
    恰恰更容易出现在弱的那一臂，等于让弱臂白占便宜。
    tokens 那四个指标不需要这层：schema 对未知 token 给的是 `None`（口径裁定 3），
    `_mean` 本来就滤掉 `None`。**两套关于"未知"的表示法并存是这个仓库的既有事实，
    不要在这里统一它们**（那要动 `normalize_row`，会连带影响 `summarize_rows`），
    只在使用点上翻译一次，并把理由写在这里。
    """
    value = _at(row, path)
    if path != "duration_s":
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
        return None
    return value


def compare_rows(
    rows_a: Sequence[Mapping[str, Any]],
    rows_b: Sequence[Mapping[str, Any]],
    *,
    labels: tuple[str, str] = ("a", "b"),
    metrics: Sequence[str] = DEFAULT_METRICS,
    dimension: Dimension | None = None,
    exclude: Sequence[str] = (),
) -> dict[str, Any]:
    """逐题行层面的对比。**它看不到报告，所以也无从做变量校验。**

    返回的 dict 里没有 `variables_seen`、`variable_mismatches` 恒为空——那不代表
    "校验通过"，而代表"无从校验"。所以这样的结果喂给 `headline()` 得到的是
    `GUARD_BLIND_WARNING` 而不是胜者（`_guard_was_blind` 把"键缺失"也判成瞎）。
    这条路径曾经绕过守卫直接报出 `mycode 多过 6 题（…）`，而 Task 12 要 import 的
    正是 `compare_rows`/`pair_rows`/`rows_of`，所以守卫必须在这一层自己生效。

    手上有报告就用 `compare_reports`（它会算出 `variables_seen`）；只有行、
    但确实在别处校验过变量的调用方可以自己把 `variables_seen` 塞进结果 dict，
    两侧都非 0 才会被当成"校验过"。
    """
    every = pair_rows(rows_a, rows_b, dimension=dimension)
    banned = set(exclude)
    excluded = sorted({p["dimension"] for p in every if p["dimension"] in banned})
    pairs = [p for p in every if p["dimension"] not in banned]
    scored = [p for p in pairs if p["a_passed"] is not None and p["b_passed"] is not None]
    unscored = [p for p in pairs if p["a_passed"] is None or p["b_passed"] is None]
    only_a = [p for p in scored if p["a_passed"] and not p["b_passed"]]
    only_b = [p for p in scored if p["b_passed"] and not p["a_passed"]]

    def rate(side: str) -> float | None:
        return round(sum(1 for p in scored if p[f"{side}_passed"]) / len(scored), 4) if scored else None

    table: dict[str, dict[str, int]] = {}
    for pair in scored:
        key = pair["dimension"] or "?"
        row = table.setdefault(key, {"n": 0, "a": 0, "b": 0})
        row["n"] += 1
        row["a"] += int(pair["a_passed"])
        row["b"] += int(pair["b_passed"])

    return {
        "labels": {"a": labels[0], "b": labels[1]},
        "benchmark": None,
        "n_paired": len(scored),
        "n_unscored": len(unscored),
        "unscored_tasks": [p["task_id"] for p in unscored],
        "a_passed": sum(1 for p in scored if p["a_passed"]),
        "b_passed": sum(1 for p in scored if p["b_passed"]),
        "a_pass_rate": rate("a"),
        "b_pass_rate": rate("b"),
        "only_a": [p["task_id"] for p in only_a],
        "only_b": [p["task_id"] for p in only_b],
        "mcnemar_p": round(mcnemar_exact(len(only_a), len(only_b)), 4),
        "means": {
            metric: {
                "a": _mean([_metric_value(p["a"], metric) for p in scored]),
                "b": _mean([_metric_value(p["b"], metric) for p in scored]),
            }
            for metric in metrics
        },
        "by_dimension": dict(sorted(table.items())),
        "excluded": excluded,
        "pairs": pairs,
        "variable_mismatches": [],
    }


def _label_of(report: Mapping[str, Any], fallback: str = "?") -> str:
    """默认标签的回退链：`run_id` → `meta.arm` → `summary.arm` → `benchmark` → `"?"`。

    **回退判断必须在 `str()` 之前做**：原来写的是 `str(report_a.get("run_id"))`，
    `str(None)` 会得到字符串 `"None"`，于是第一层永远"取到了"——盘上所有旧 harbor
    报告都没有 `run_id` 这个键，markdown 标题因此渲染成 `# None vs None`、
    headline 渲染成 `None 15/24 vs None 10/24`（评审在已发表那一对真报告上实测过）。
    拿不到身份就老实退到 `"?"`：一个看得见的占位符好过一个像名字的假值。

    三个来源都在盘上真实存在：新报告的 `meta` 里有 `arm`（实测探测批报告的 meta 键集是
    `arm, config_path, git_revision, model, run_dir, seed, stage, task_ids, variables`），
    旧报告的 `arm` 在 `summary` 里。harbor 自己的 CLI 是用**文件名主干**当标签的
    （`eval/harbor/compare.py`），但这里只拿得到 dict、拿不到路径，所以指望不上文件主干。
    """
    meta = report.get("meta")
    summary = report.get("summary")
    candidates = (
        report.get("run_id"),
        meta.get("arm") if isinstance(meta, Mapping) else None,
        summary.get("arm") if isinstance(summary, Mapping) else None,
        report.get("benchmark"),
    )
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value)
    return fallback


def compare_reports(
    report_a: Mapping[str, Any],
    report_b: Mapping[str, Any],
    *,
    labels: tuple[str, str] | None = None,
    dimension: Dimension | None = None,
    exclude: Sequence[str] = (),
) -> dict[str, Any]:
    result = compare_rows(
        rows_of(report_a),
        rows_of(report_b),
        labels=labels or (_label_of(report_a), _label_of(report_b)),
        dimension=dimension,
        exclude=exclude,
    )
    result["benchmark"] = report_a.get("benchmark")
    result["a_report"] = report_a.get("run_id")
    result["b_report"] = report_b.get("run_id")
    result["variable_mismatches"] = [asdict(m) for m in variable_mismatches(report_a, report_b)]
    # 守卫到底看了几个变量。两侧都是 0 时 `variable_mismatches` 也是空的，
    # 但那是"没得校验"不是"校验通过"，headline 要能把这两种空区分开。
    result["variables_seen"] = {
        "a": len(_variables_of(report_a)),
        "b": len(_variables_of(report_b)),
    }
    return result


def _guard_was_blind(comparison: Mapping[str, Any]) -> bool:
    """True = 这次对比没有做过有效的变量校验，所以不许下结论。

    两种情况都算瞎：
    - `variables_seen` 根本不存在——调用方走的是 `compare_rows`，它只看得到行、
      看不到报告，所以**无从校验**。"没校验"和"校验通过"必须给出不同的话，
      否则 `compare_rows` + `headline` 这条公开路径就能在零校验下报出胜者
      （修复前它印的是 `mycode 多过 6 题（mycode 6/6 vs opencode 0/6，…）`）。
    - 任一侧记录的变量数为 0——`all` 而不是 `any`：一侧没记就等于没法比对，
      而 1.86× 那次事故正是这个形状（两份报告都没记下思考是关的）。
    """
    seen = comparison.get("variables_seen")
    return not isinstance(seen, Mapping) or not all(seen.values())


def _guard_blind_reason(comparison: Mapping[str, Any], label_a: str, label_b: str) -> str:
    """守卫为什么瞎——给 `GUARD_BLIND_WARNING` 补一个**在当前调用层为真**的原因。

    原来那句写死"两份报告都没有记录控制变量"，在 `compare_rows` 这一层是假的：
    那一层根本拿不到报告。一句在某个调用层为假的话不该被继续复用，所以原因按
    实际拿到的东西说。
    """
    seen = comparison.get("variables_seen")
    if not isinstance(seen, Mapping):
        return "这一层只看得到逐题行、看不到报告，无从校验控制变量"
    empty = [label for side, label in (("a", label_a), ("b", label_b)) if not seen.get(side)]
    if len(empty) == 2:
        return f"{empty[0]} 与 {empty[1]} 都{GUARD_NO_VARIABLES_NOTE}"
    if empty:
        return f"{empty[0]} 一侧{GUARD_NO_VARIABLES_NOTE}"
    return GUARD_NO_VARIABLES_NOTE  # 走不到：判成瞎就意味着至少一侧为 0


def headline(comparison: Mapping[str, Any]) -> str:
    """一句话结论。变量没对齐时只给描述性数字——这是守卫的执行环节。

    两个守卫分支（未对齐 / 未经校验）的输出被
    `tests/unit/test_eval_compare.py` 钉成**金标准串**：逐字符相等。
    这是安全关键输出，测试与生产各存一份是特性不是缺陷——改措辞就必须有意识地改测试。
    """
    label_a = comparison["labels"]["a"]
    label_b = comparison["labels"]["b"]
    n = comparison["n_paired"]
    p = comparison["mcnemar_p"]
    descriptive = (
        f"{label_a} {comparison['a_passed']}/{n} vs {label_b} {comparison['b_passed']}/{n}，"
        f"McNemar p = {p}"
    )
    mismatches = comparison.get("variable_mismatches") or []
    if mismatches:
        return (
            f"变量未对齐（{len(mismatches)} 项不同）：{descriptive}。"
            f"下面只是描述性数字，不构成结论。"
        )
    if _guard_was_blind(comparison):
        # 不给结论性措辞：说"未经过变量校验"的同一句话里说"谁更强"是自相矛盾，
        # 而 1.86× 那次事故正是这个形状——没人记下思考是关的。
        return (
            f"{GUARD_BLIND_WARNING}（{_guard_blind_reason(comparison, label_a, label_b)}）："
            f"{descriptive}。下面只是描述性数字，不构成结论。"
        )
    # 能走到这里就说明 `variables_seen` 是个 Mapping、两侧都非 0、且一项差异都没有，
    # 也就是变量**真的**比过而且一致——所以这句前缀由上面两个分支保住，不是客套。
    aligned = "变量已对齐。"
    if comparison["only_a"] and not comparison["only_b"]:
        return f"{aligned}{label_a} 多过 {len(comparison['only_a'])} 题（{descriptive}）"
    if comparison["only_b"] and not comparison["only_a"]:
        return f"{aligned}{label_b} 多过 {len(comparison['only_b'])} 题（{descriptive}）"
    return f"{aligned}没测出差别：{descriptive}"


def render_markdown(comparison: Mapping[str, Any]) -> str:
    label_a, label_b = comparison["labels"]["a"], comparison["labels"]["b"]
    mismatches = comparison.get("variable_mismatches") or []
    lines = [f"# {label_a} vs {label_b}", ""]

    if mismatches:
        # 这段文字里不能出现任何胜者措辞：它是"变量未对齐"时唯一一定会被读到的地方，
        # 写进去就等于自己把守卫拆了。禁词清单（`SUPERLATIVES` / `WINNER_PHRASES`）
        # 只在 `tests/unit/test_eval_compare.py` 里存一份——那是**真的会被执行**的那份，
        # 外加下面两条横幅的金标准串断言（多一个字、少一个字都红）。
        lines += [
            "> ⚠️ **变量未对齐**——下面所有数字都是描述性的，不构成结论。",
            "> 对齐之后重跑，或者只把它当线索。",
            "",
            "| 变量 | " + label_a + " | " + label_b + " |",
            "| --- | --- | --- |",
        ]
        lines += [f"| `{m['field']}` | `{m['a']}` | `{m['b']}` |" for m in mismatches]
        lines.append("")
    elif _guard_was_blind(comparison):
        lines += [
            f"> {GUARD_BLIND_WARNING}（{_guard_blind_reason(comparison, label_a, label_b)}）。",
            "> 下面所有数字都是描述性的，不构成结论。",
            "",
        ]

    lines += [
        headline(comparison),
        "",
        f"- 配对题数：{comparison['n_paired']}"
        + (f"，另有 {comparison['n_unscored']} 题没判分（{', '.join(comparison['unscored_tasks'])}）"
           if comparison["n_unscored"] else ""),
        f"- 通过率：{label_a} {comparison['a_pass_rate']} vs {label_b} {comparison['b_pass_rate']}",
        f"- 只有 {label_a} 过：{len(comparison['only_a'])} 题"
        + (f"（{', '.join(comparison['only_a'])}）" if comparison["only_a"] else ""),
        f"- 只有 {label_b} 过：{len(comparison['only_b'])} 题"
        + (f"（{', '.join(comparison['only_b'])}）" if comparison["only_b"] else ""),
        f"- McNemar 精确检验双侧 p = {comparison['mcnemar_p']}",
    ]
    if comparison.get("excluded"):
        lines.append(f"- 已整组剔除：{', '.join(comparison['excluded'])}")
    lines += ["", "| 指标 | " + label_a + " | " + label_b + " |", "| --- | --- | --- |"]
    for metric, sides in comparison["means"].items():
        lines.append(f"| 平均 `{metric}` | {sides['a']} | {sides['b']} |")

    if comparison["by_dimension"]:
        lines += ["", "## 按维度", "", f"| 维度 | 题数 | {label_a} 过 | {label_b} 过 |",
                  "| --- | --- | --- | --- |"]
        for key, row in comparison["by_dimension"].items():
            lines.append(f"| {key} | {row['n']} | {row['a']} | {row['b']} |")

    lines += ["", "## 逐题", "", f"| 题目 | {label_a} | {label_b} |", "| --- | --- | --- |"]
    for pair in comparison["pairs"]:
        if pair["a_passed"] is None and pair["b_passed"] is None:
            continue
        lines.append(f"| {pair['task_id']} | {_mark(pair['a_passed'])} | {_mark(pair['b_passed'])} |")
    lines += ["", "> `?` = 没判分，不计入配对统计。"]
    return "\n".join(lines) + "\n"


def _mark(value: bool | None) -> str:
    if value is None:
        return "?"
    return "过" if value else "没过"
