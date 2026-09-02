from __future__ import annotations

from .eval_report import (
    evaluate_online_skill_evolution,
    evaluate_online_skill_evolution_async,
    format_online_skill_eval,
    format_online_skill_eval_async,
)

__all__ = [
    "evaluate_online_skill_evolution",
    "evaluate_online_skill_evolution_async",
    "format_online_skill_eval",
    "format_online_skill_eval_async",
]


if __name__ == "__main__":
    print(format_online_skill_eval())
