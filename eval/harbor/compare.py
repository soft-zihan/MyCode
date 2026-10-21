"""两臂对比：把 MyCode 和 opencode 的 harbor 报告配成对，算差异和显著性。

统计（配对、McNemar 精确检验、分层）在 `eval/common/compare.py`，这里**只做词汇映射**：
通用层说 a/b，harbor 说 mycode/opencode；通用层说 `tokens.input`，harbor 说
`mycode_mean_input_tokens`。保留这一层的理由是 README §5 和既有测试用的都是
harbor 的词汇，而改成 a/b 收益为零。

两臂跑的是同一批题、同一个模型、同一套判分，所以是**配对设计**——差异只在 agent
本身。配对数据该用 McNemar（只看两臂判定不一致的题），而不是两个独立比例的 z 检验。

**变量守卫**：本模块曾经只会算数字、不会问"这两臂的控制变量一样吗"，于是
"MyCode 快 1.9×"这个假结论（两臂的思考开关根本没对齐）一路活到了汇报环节。
现在守卫来自通用层，harbor 这一层负责把它渲染出来：开火时 markdown 顶部打横幅，
CLI 的 stdout 也回显同样的几句话——**stdout 那行是人唯一一定会读到的输出，
只在 markdown 里说等于没说**。措辞两处都取自通用层的 `guard_banner`，一份都不抄。
注意**行层面的 `compare()` 拿不到报告，所以无从校验变量**——CLI 走的是
`compare_run_reports()`，两条路的区别不是风格问题：走错了守卫就静默失效，
而静默失效的守卫在输出上和"校验通过"长得一模一样。

用法::

    .venv/bin/python -m eval.harbor.compare --stage final --exclude-language java
    .venv/bin/python -m eval.harbor.compare --mycode eval/reports/a.json --opencode eval/reports/b.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

# 守卫横幅的措辞只有通用层那一份：`guard_banner` 是它的公开入口，本模块只负责
# 把 harbor 的词汇（`mycode_label` / `opencode_label`）喂给它。措辞是安全关键输出
# （"`None`" 和 "未记录" 是两件事——前者把未知说成已知取值，正是 1.9× 假结论的
# 人眼版），在 harbor 这边抄一份就一定会各自漂移。这里曾经 import 通用层三个私有符号
# （`_guard_blind_reason` / `_guard_was_blind` / `_mismatch_cell`）自己拼横幅，
# 结果三句里两句各写了一份、还漏了一句补救提示（Task 12 复审 I-2 / M-3）。
from eval.common.compare import (
    compare_reports,
    compare_rows,
    guard_banner,
    pair_rows,
)
from eval.common.schema import normalize_row
from eval.harbor.runner import task_language

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT / "eval" / "reports"
REPORT_NAME_RE = re.compile(r"^harbor_(?P<arm>[a-z]+)_(?P<stage>[a-z]+)_(?P<stamp>\d{8}__\d{6})\.json$")

# harbor 只报这三项：token 两项 + agent 执行时长。装 agent 的时长是 harness 开销
# （pip 装 1.35 MB 本地依赖 vs npm 装 opencode），放进对比只会被误读成 scaffold 优劣
HARBOR_METRICS = ("tokens.input", "tokens.output", "duration_s")

_MEAN_KEYS = {
    "tokens.input": ("mycode_mean_input_tokens", "opencode_mean_input_tokens"),
    "tokens.output": ("mycode_mean_output_tokens", "opencode_mean_output_tokens"),
    "duration_s": ("mycode_mean_seconds", "opencode_mean_seconds"),
}

# 两臂不是同一秒起跑的，git_revision 记录的是"哪一版 MyCode 编排了这一臂"，
# 不是"哪一版 agent 代码在跑"。把它算成"变量没对齐"，横幅就会永远挂着，
# 而永远挂着的警告等于没有警告。
#
# **只有这一项，不许"多留一条以防万一"**。这里曾经还挂着一条 `"variables.arm"`，
# 实测是死条目：`arm` 不在 `run_variables()` 返回的 8 个键里
# （`git_revision, model, mycode_env, provider, seed, stage, thinking_effective,
# thinking_mode`，见 `eval/harbor/runner.py` 的 `run_variables`），它只写进
# `meta.arm` / `summary.arm`，而通用层的 `variable_mismatches` **不比这两处**
# （它比 `benchmark`、`_META_BACKED_VARIABLES` 三项、`meta.task_ids`、
# 以及 `_variables_of` 两边键的并集）。盘上两对真报告实测也印证了：探测批
# 0 项 mismatch，已发表那一对 8 项、field 里没有 `variables.arm`。
# 一份看着在守、其实永远不触发的清单，会让人以为 arm 已经被处理了。
ARM_KEYS = frozenset({"meta.git_revision"})


def load_report(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_latest_report(arm: str, stage: str, reports_dir: Path | str = REPORTS_DIR) -> Path:
    """按文件名里的时间戳挑最新的一份报告。"""
    reports_dir = Path(reports_dir)
    matches: list[tuple[str, Path]] = []
    for path in reports_dir.glob(f"harbor_{arm}_{stage}_*.json"):
        found = REPORT_NAME_RE.match(path.name)
        if found:
            matches.append((found.group("stamp"), path))
    if not matches:
        raise FileNotFoundError(
            f"{reports_dir} 里没有 {arm}/{stage} 的报告；先跑 "
            f"`python -m eval.harbor.runner --arm {arm} --stage {stage}`"
        )
    return max(matches)[1]


def passed(trial: Mapping[str, Any]) -> bool | None:
    """True/False = 判过分的通过与否；None = 没有可用判定，不能当成失败。

    口径的唯一来源是 `schema` 的 harbor 适配器（`reward >= PASSED_THRESHOLD`，
    cpp 那类「verifier 没写出 reward 文件」的 trial 已经在 `collect_trials` 里按 0 分记进
    reward 了，异常类型只作为证据保留在报告里）。这里只做归一化后取值，不再自己推一遍：
    本模块曾经自己从 reward 推，于是同一件事有两处实现、而生产路径只走 schema 那一处，
    改 schema 不会让这里的测试变红——测试就成了安慰剂。
    """
    return normalize_row(trial, benchmark="harbor")["passed"]


def language_of(task_id: str, row: Mapping[str, Any] | None = None) -> str | None:
    """`common.pair_rows` 要的 dimension 回调：按语言分层。

    抽样本来就是按语言均衡的，整体数会把它抹平。
    """
    return task_language(task_id)


def _rows(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """既吃 harbor 原始 trial，也吃已归一化的行。"""
    return [
        dict(item) if "task_id" in item else normalize_row(item, benchmark="harbor")
        for item in items
    ]


def _harbor_pair(pair: Mapping[str, Any]) -> dict[str, Any]:
    """通用层的一对（a/b + 原始行）→ harbor 的一对（mycode/opencode + 平铺字段）。"""
    a, b = pair["a"], pair["b"]

    def side(row: Mapping[str, Any] | None, path: str) -> Any:
        cur: Any = row
        for part in path.split("."):
            if not isinstance(cur, Mapping):
                return None
            cur = cur.get(part)
        return cur

    return {
        "task_name": pair["task_id"],
        "language": pair["dimension"],
        "mycode_passed": pair["a_passed"],
        "opencode_passed": pair["b_passed"],
        # `error` 由 schema 从 `exception_type` + `exception_message` 合成，
        # 所以"出过异常没"这一位和原来的 `bool(trial["exception_type"])` 等价
        "mycode_errored": bool(side(a, "error")),
        "opencode_errored": bool(side(b, "error")),
        "mycode_input_tokens": side(a, "tokens.input"),
        "opencode_input_tokens": side(b, "tokens.input"),
        "mycode_output_tokens": side(a, "tokens.output"),
        "opencode_output_tokens": side(b, "tokens.output"),
        "mycode_seconds": side(a, "duration_s"),
        "opencode_seconds": side(b, "duration_s"),
    }


def _harbor_result(
    generic: Mapping[str, Any], mycode_label: str, opencode_label: str
) -> dict[str, Any]:
    """通用层的对比结果 → harbor 的词汇。**只翻译，不算任何统计。**

    `means` 里必须有 `_MEAN_KEYS` 的三项。走 `compare_rows` 时由 `HARBOR_METRICS`
    保证；走 `compare_reports` 时由通用层的 `DEFAULT_METRICS` 保证——它是
    `HARBOR_METRICS` 的超集（`compare_reports` 不接受 `metrics=`，已报给控制器）。
    哪天通用层收窄了 `DEFAULT_METRICS`，这里会当场 KeyError，不会静默出一张空表。
    """
    result: dict[str, Any] = {
        "mycode_label": mycode_label,
        "opencode_label": opencode_label,
        "excluded_languages": generic["excluded"],
        "n_paired": generic["n_paired"],
        "n_unscored": generic["n_unscored"],
        "unscored_tasks": generic["unscored_tasks"],
        "mycode_passed": generic["a_passed"],
        "opencode_passed": generic["b_passed"],
        "mycode_pass_rate": generic["a_pass_rate"],
        "opencode_pass_rate": generic["b_pass_rate"],
        "only_mycode": generic["only_a"],
        "only_opencode": generic["only_b"],
        "mcnemar_p": generic["mcnemar_p"],
        "pairs": [_harbor_pair(pair) for pair in generic["pairs"]],
        "variable_mismatches": list(generic.get("variable_mismatches") or []),
    }
    if "variables_seen" in generic:
        # 键缺失本身就是"没校验过"的信号，`_guard_was_blind` 就是这么判的。
        # 所以行层面那条路**不许**在这里补一个 `{"a": 0, "b": 0}` 之类的假值：
        # 补了就是把"无从校验"伪装成"校验过但一侧记了 0 个"，措辞会说错话。
        result["variables_seen"] = generic["variables_seen"]
    for metric, (mine_key, theirs_key) in _MEAN_KEYS.items():
        result[mine_key] = generic["means"][metric]["a"]
        result[theirs_key] = generic["means"][metric]["b"]
    return result


def pair_tasks(
    mine: Sequence[Mapping[str, Any]], theirs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """按题目名把两臂配成对；只有一臂跑过的题单独列出来，不混进配对统计。"""
    return [
        _harbor_pair(pair)
        for pair in pair_rows(_rows(mine), _rows(theirs), dimension=language_of)
    ]


def drop_expected_mismatches(result: dict[str, Any]) -> dict[str, Any]:
    """摘掉"两臂本来就该不同"的变量（见 `ARM_KEYS`）。原地改并返回同一个 dict。"""
    result["variable_mismatches"] = [
        m for m in result.get("variable_mismatches", []) if m["field"] not in ARM_KEYS
    ]
    return result


def compare(
    mine: Sequence[Mapping[str, Any]],
    theirs: Sequence[Mapping[str, Any]],
    *,
    mycode_label: str = "mycode",
    opencode_label: str = "opencode",
    exclude_languages: Sequence[str] = (),
) -> dict[str, Any]:
    """**行层面**的两臂对比：统计全部委托给 `common.compare_rows`。

    这一层只看得到逐题行、看不到报告，所以 `variable_mismatches` 恒为空、
    `variables_seen` 这个键根本不存在——那**不代表校验通过，代表无从校验**。
    手上有报告就用 `compare_run_reports`，否则守卫是空的。
    """
    return _harbor_result(
        compare_rows(
            _rows(mine),
            _rows(theirs),
            labels=(mycode_label, opencode_label),
            metrics=HARBOR_METRICS,
            dimension=language_of,
            exclude=exclude_languages,
        ),
        mycode_label,
        opencode_label,
    )


def compare_run_reports(
    mine_report: Mapping[str, Any],
    theirs_report: Mapping[str, Any],
    *,
    mycode_label: str = "mycode",
    opencode_label: str = "opencode",
    exclude_languages: Sequence[str] = (),
) -> dict[str, Any]:
    """**报告层面**的两臂对比：统计与变量守卫都来自 `common.compare_reports`。

    和 `compare()` 的唯一区别就是守卫有没有数据，而这个区别是本模块存在的理由：
    `compare()` 走 `compare_rows`，`variable_mismatches` 恒为空、`variables_seen`
    键不存在；只有这一条路会真的去比 `meta.variables` / `summary.variables`。
    CLI 走这一条。已经摘掉 `ARM_KEYS`（否则横幅永远挂着）。
    """
    return drop_expected_mismatches(
        _harbor_result(
            compare_reports(
                mine_report,
                theirs_report,
                labels=(mycode_label, opencode_label),
                dimension=language_of,
                exclude=exclude_languages,
            ),
            mycode_label,
            opencode_label,
        )
    )


def by_language(pairs: Sequence[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """按语言分层看两臂各过了几题。抽样本来就是按语言均衡的，整体数会把它抹平。"""
    table: dict[str, dict[str, int]] = {}
    for pair in pairs:
        if pair["mycode_passed"] is None or pair["opencode_passed"] is None:
            continue
        row = table.setdefault(pair["language"] or "?", {"n": 0, "mycode": 0, "opencode": 0})
        row["n"] += 1
        row["mycode"] += int(pair["mycode_passed"])
        row["opencode"] += int(pair["opencode_passed"])
    return dict(sorted(table.items()))


def _guard_lines(result: Mapping[str, Any]) -> list[str]:
    """守卫横幅。措辞的唯一来源是通用层的 `guard_banner`——这里**一句都不自己写**。

    留这一层薄壳（而不是让两个调用点直接调 `guard_banner`）只做一件事：把 harbor 的
    词汇翻成通用层的入参。harbor 的结果字典用的是 `mycode_label` / `opencode_label`、
    **没有 `labels` 键**，所以两个标签必须显式传。

    守卫没开火（变量真比过而且一致）时返回 `[]`，markdown 与 stdout 都不会有横幅。
    `variables_seen` 这个键在行层面的结果里**根本不存在**，`guard_banner` 把键缺失判成
    "瞎"、照样打横幅——所以这里不许给它补一个 `{"a": 0, "b": 0}` 之类的假值
    （见 `_harbor_result` 里那段注释）。
    """
    return guard_banner(
        result,
        label_a=result["mycode_label"],
        label_b=result["opencode_label"],
    )


def render_markdown(result: dict[str, Any]) -> str:
    pairs = [p for p in result["pairs"] if p["mycode_passed"] is not None or p["opencode_passed"] is not None]
    excluded = result.get("excluded_languages") or []
    lines = [
        f"# MyCode vs opencode（{result['mycode_label']} / {result['opencode_label']}）",
        "",
    ]
    lines += _guard_lines(result)
    if excluded:
        lines += [
            f"> **已剔除语言：{', '.join(excluded)}**（理由见 eval/harbor/README.md 第 4 节："
            "java 题的 Gradle wrapper 在本环境里 TLS 验不过，两臂都必然拿 0，测不到东西）",
            "",
        ]
    lines += [
        f"- 配对题数：{result['n_paired']}"
        + (f"，另有 {result['n_unscored']} 题没判分（{', '.join(result['unscored_tasks'])}）"
           if result["n_unscored"] else ""),
        f"- 通过：MyCode {result['mycode_passed']}/{result['n_paired']}"
        f"（{result['mycode_pass_rate']}） vs opencode {result['opencode_passed']}/{result['n_paired']}"
        f"（{result['opencode_pass_rate']}）",
        f"- 只有 MyCode 过：{len(result['only_mycode'])} 题"
        + (f"（{', '.join(result['only_mycode'])}）" if result["only_mycode"] else ""),
        f"- 只有 opencode 过：{len(result['only_opencode'])} 题"
        + (f"（{', '.join(result['only_opencode'])}）" if result["only_opencode"] else ""),
        f"- McNemar 精确检验双侧 p = {result['mcnemar_p']}",
        "",
        "| 指标 | MyCode | opencode |",
        "| --- | --- | --- |",
        f"| 平均输入 token | {result['mycode_mean_input_tokens']} | {result['opencode_mean_input_tokens']} |",
        f"| 平均输出 token | {result['mycode_mean_output_tokens']} | {result['opencode_mean_output_tokens']} |",
        f"| 平均 agent 执行秒数 | {result['mycode_mean_seconds']} | {result['opencode_mean_seconds']} |",
        "",
        "## 按语言",
        "",
        "| 语言 | 题数 | MyCode 过 | opencode 过 |",
        "| --- | --- | --- | --- |",
    ]
    for language, row in by_language(result["pairs"]).items():
        lines.append(f"| {language} | {row['n']} | {row['mycode']} | {row['opencode']} |")
    lines += ["", "## 逐题", "", "| 题目 | MyCode | opencode |", "| --- | --- | --- |"]
    for pair in pairs:
        lines.append(
            f"| {pair['task_name']} | {_mark(pair['mycode_passed'])} | {_mark(pair['opencode_passed'])} |"
        )
    lines += [
        "",
        "> `?` = 出异常没判分，不计入配对统计。判分口径是 reward ≥ 1.0"
        "（aider-polyglot 的测试全过才算过）。",
    ]
    return "\n".join(lines) + "\n"


def _mark(value: bool | None) -> str:
    if value is None:
        return "?"
    return "过" if value else "没过"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.harbor.compare", description="MyCode vs opencode 两臂配对对比"
    )
    parser.add_argument("--stage", default="probe", help="没给 --mycode/--opencode 时按这个找最新报告")
    parser.add_argument("--mycode", default=None, help="MyCode 臂的报告 JSON")
    parser.add_argument("--opencode", default=None, help="opencode 臂的报告 JSON")
    parser.add_argument("--out", default=None, help="输出 markdown 路径；默认写进 eval/reports")
    parser.add_argument(
        "--exclude-language",
        action="append",
        default=[],
        metavar="LANG",
        help="把某语言的题整组剔出统计，可重复。留着这个开关是为了跟 java 修好之前的批次对齐"
        "（那时 java 题因 JVM 不读 HTTPS_PROXY 而两臂全灭，见 README 第 4 节）；"
        "修好之后就别再剔了——225 题基线上 java 是 12:11，剔掉反而丢掉七分之一样本",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mine_path = Path(args.mycode) if args.mycode else find_latest_report("mycode", args.stage)
    theirs_path = (
        Path(args.opencode) if args.opencode else find_latest_report("opencode", args.stage)
    )
    mine, theirs = load_report(mine_path), load_report(theirs_path)

    # 必须是 `compare_run_reports`（吃报告）而不是 `compare`（吃行）：
    # 只有前者能算出 `variable_mismatches` / `variables_seen`，也就是守卫的唯一数据来源。
    result = compare_run_reports(
        mine,
        theirs,
        mycode_label=f"{mine_path.stem}",
        opencode_label=f"{theirs_path.stem}",
        exclude_languages=args.exclude_language,
    )
    result["mycode_report"] = str(mine_path)
    result["opencode_report"] = str(theirs_path)

    excluded = result["excluded_languages"]
    suffix = f"-no{'+'.join(excluded)}" if excluded else ""
    out_path = (
        Path(args.out)
        if args.out
        else REPORTS_DIR
        / f"harbor_compare_{args.stage}{suffix}_{datetime.now():%Y%m%d__%H%M%S}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(result), encoding="utf-8")
    (out_path.with_suffix(".json")).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 守卫开火时 stdout 也必须说。下面那行金丝雀是人跑这条命令**唯一一定会读到**的
    # 输出，而「MyCode 13/20 vs opencode 8/20，McNemar p=0.0625」正是当年「快 1.9×」
    # 被汇报上去的那种一行式摘要——守卫只写在 markdown 里，等于沉默地给出一个看起来
    # 可信的结论，那是本模块族唯一的严重失效模式。
    #
    # 措辞同样只有通用层那一份：这里回显的就是 `_guard_lines()` 里 `> ` 开头的几句
    # （stdout 是纯文本，所以去掉引用标记与 markdown 粗体），一句都不自己写。
    # 差异表不回显——它是 markdown 表格，在纯文本里读不成样子，而金丝雀那行已经
    # 带着 markdown 的路径。守卫没开火时 `_guard_lines()` 返回 `[]`，一行都不打：
    # 每次都打的警告是噪音，等于没有警告。
    #
    # 只加行、不动下面那行金丝雀：实施计划 Step 5 是"重定向到文件后用 Read 工具肉眼看"、
    # Task 14 简报 P3 是散文 `Expected:`，两处都不是全行相等断言，而且期望串是实际
    # stdout 的严格子串（实测见 task-12-round1-report.md）。
    for line in _guard_lines(result):
        if line.startswith("> "):
            print(line.removeprefix("> ").replace("**", ""))
    print(
        "MyCode {mp}/{n} vs opencode {op}/{n}，McNemar p={p} → {out}".format(
            mp=result["mycode_passed"],
            op=result["opencode_passed"],
            n=result["n_paired"],
            p=result["mcnemar_p"],
            out=out_path,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
