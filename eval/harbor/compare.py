"""两臂对比：把 MyCode 和 opencode 的 harbor 报告配成对，算差异和显著性。

两臂跑的是同一批题、同一个模型、同一套判分，所以是**配对设计**——差异只在 agent
本身。配对数据该用 McNemar（只看两臂判定不一致的题），而不是两个独立比例的 z 检验。

用法::

    .venv/bin/python -m eval.harbor.compare --stage probe
    .venv/bin/python -m eval.harbor.compare --mycode eval/reports/a.json --opencode eval/reports/b.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from math import comb
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT / "eval" / "reports"
REPORT_NAME_RE = re.compile(r"^harbor_(?P<arm>[a-z]+)_(?P<stage>[a-z]+)_(?P<stamp>\d{8}__\d{6})\.json$")

PASSED_THRESHOLD = 1.0


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


def report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """取这次运行的逐题行。

    新报告是统一信封（`results`，行已是标准核心）；2026-10-18 之前落盘的是
    `trials`（harbor 原始词汇）。两种都要能读——那些报告重跑一次要 45 分钟
    网关时间，而 §5 的结论就引自它们。
    """
    rows = report.get("results")
    if rows is not None:
        # 归一化后 task_name 变成了 task_id、n_*_tokens 变成了 tokens.*，
        # 这里还原成 pair_tasks 认的形状，统计逻辑就只有一份
        return [_as_trial(row) for row in rows]
    return report.get("trials") or []


def _as_trial(row: dict[str, Any]) -> dict[str, Any]:
    if "task_name" in row:  # 已经是原始 trial
        return row
    extra = row.get("extra") or {}
    tokens = row.get("tokens") or {}
    return {
        "task_name": row.get("task_id"),
        "reward": row.get("reward"),
        "exception_type": extra.get("exception_type"),
        "agent_execution_s": row.get("duration_s"),
        "n_input_tokens": tokens.get("input"),
        "n_output_tokens": tokens.get("output"),
        "n_cache_tokens": tokens.get("cached"),
        **{k: v for k, v in extra.items() if k not in {"exception_type"}},
    }


def passed(trial: dict[str, Any]) -> bool | None:
    """True/False = 判过分的通过与否；None = 没有可用判定，不能当成失败。

    只看 `reward`：cpp 那类「verifier 没写出 reward 文件」的 trial 已经在
    `collect_trials` 里按 0 分记进 reward 了，异常类型只作为证据保留在报告里。
    """
    reward = trial.get("reward")
    if reward is None:
        return None
    return reward >= PASSED_THRESHOLD


def by_task(trials: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """题目名 → trial。同名多题（多 attempt）时留最后一个，也就是最新那次。"""
    return {trial["task_name"]: trial for trial in trials if trial.get("task_name")}


def pair_tasks(
    mine: Sequence[dict[str, Any]], theirs: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按题目名把两臂配成对；只有一臂跑过的题单独列出来，不混进配对统计。"""
    left, right = by_task(mine), by_task(theirs)
    pairs: list[dict[str, Any]] = []
    for task_name in sorted(set(left) | set(right)):
        mine_trial, theirs_trial = left.get(task_name), right.get(task_name)
        pairs.append(
            {
                "task_name": task_name,
                "language": task_name.split("_")[1] if task_name.startswith("polyglot_") else None,
                "mycode_passed": passed(mine_trial) if mine_trial else None,
                "opencode_passed": passed(theirs_trial) if theirs_trial else None,
                "mycode_errored": bool(mine_trial and mine_trial.get("exception_type")),
                "opencode_errored": bool(theirs_trial and theirs_trial.get("exception_type")),
                "mycode_input_tokens": (mine_trial or {}).get("n_input_tokens"),
                "opencode_input_tokens": (theirs_trial or {}).get("n_input_tokens"),
                "mycode_output_tokens": (mine_trial or {}).get("n_output_tokens"),
                "opencode_output_tokens": (theirs_trial or {}).get("n_output_tokens"),
                "mycode_seconds": (mine_trial or {}).get("agent_execution_s"),
                "opencode_seconds": (theirs_trial or {}).get("agent_execution_s"),
            }
        )
    return pairs


def mcnemar_exact(only_left: int, only_right: int) -> float:
    """McNemar 精确检验的双侧 p 值。

    只有「两臂判定不一致」的题携带信息：a 臂过 b 臂没过 = only_left，反之 = only_right。
    两臂都过或都没过的题在这个检验里不出现，所以 25 题里可能只有几题进入检验，
    p 值自然大——那是样本量的事实，不是没差别。
    """
    n = only_left + only_right
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(only_left, only_right) + 1)) / 2**n
    return min(1.0, 2 * tail)


def _mean(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known) / len(known), 1) if known else None


def compare(
    mine: Sequence[dict[str, Any]],
    theirs: Sequence[dict[str, Any]],
    *,
    mycode_label: str = "mycode",
    opencode_label: str = "opencode",
    exclude_languages: Sequence[str] = (),
) -> dict[str, Any]:
    pairs = pair_tasks(mine, theirs)
    excluded = sorted({p["language"] for p in pairs if p["language"] in set(exclude_languages)})
    if excluded:
        pairs = [p for p in pairs if p["language"] not in excluded]
    both_scored = [
        pair
        for pair in pairs
        if pair["mycode_passed"] is not None and pair["opencode_passed"] is not None
    ]
    only_mycode = [p for p in both_scored if p["mycode_passed"] and not p["opencode_passed"]]
    only_opencode = [p for p in both_scored if p["opencode_passed"] and not p["mycode_passed"]]
    unscored = [p for p in pairs if p not in both_scored]

    def rate(flag: str) -> float | None:
        return (
            round(sum(1 for p in both_scored if p[flag]) / len(both_scored), 4)
            if both_scored
            else None
        )

    return {
        "mycode_label": mycode_label,
        "opencode_label": opencode_label,
        "excluded_languages": excluded,
        "n_paired": len(both_scored),
        "n_unscored": len(unscored),
        "unscored_tasks": [p["task_name"] for p in unscored],
        "mycode_passed": sum(1 for p in both_scored if p["mycode_passed"]),
        "opencode_passed": sum(1 for p in both_scored if p["opencode_passed"]),
        "mycode_pass_rate": rate("mycode_passed"),
        "opencode_pass_rate": rate("opencode_passed"),
        "only_mycode": [p["task_name"] for p in only_mycode],
        "only_opencode": [p["task_name"] for p in only_opencode],
        "mcnemar_p": round(mcnemar_exact(len(only_mycode), len(only_opencode)), 4),
        "mycode_mean_input_tokens": _mean([p["mycode_input_tokens"] for p in both_scored]),
        "opencode_mean_input_tokens": _mean([p["opencode_input_tokens"] for p in both_scored]),
        "mycode_mean_output_tokens": _mean([p["mycode_output_tokens"] for p in both_scored]),
        "opencode_mean_output_tokens": _mean([p["opencode_output_tokens"] for p in both_scored]),
        "mycode_mean_seconds": _mean([p["mycode_seconds"] for p in both_scored]),
        "opencode_mean_seconds": _mean([p["opencode_seconds"] for p in both_scored]),
        "pairs": pairs,
    }


def by_language(pairs: Sequence[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """按语言分层看两臂各过了几题。抽样本来就是按语言均衡的，整体数会把它抹平。"""
    table: dict[str, dict[str, int]] = {}
    for pair in pairs:
        if pair["mycode_passed"] is None or pair["opencode_passed"] is None:
            continue
        row = table.setdefault(
            pair["language"] or "?", {"n": 0, "mycode": 0, "opencode": 0}
        )
        row["n"] += 1
        row["mycode"] += int(pair["mycode_passed"])
        row["opencode"] += int(pair["opencode_passed"])
    return dict(sorted(table.items()))


def render_markdown(result: dict[str, Any]) -> str:
    pairs = [p for p in result["pairs"] if p["mycode_passed"] is not None or p["opencode_passed"] is not None]
    excluded = result.get("excluded_languages") or []
    lines = [
        f"# MyCode vs opencode（{result['mycode_label']} / {result['opencode_label']}）",
        "",
    ]
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
        help="把某语言的题整组剔出统计，可重复。java 题在本环境里两臂都必然失败（README 第 4 节），"
        "剔掉才看得出真正测到的那部分",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mine_path = Path(args.mycode) if args.mycode else find_latest_report("mycode", args.stage)
    theirs_path = (
        Path(args.opencode) if args.opencode else find_latest_report("opencode", args.stage)
    )
    mine, theirs = load_report(mine_path), load_report(theirs_path)

    result = compare(
        report_rows(mine),
        report_rows(theirs),
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
