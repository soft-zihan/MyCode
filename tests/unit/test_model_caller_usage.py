"""流式 usage 的字段搬运：漏一个字段不会报错，只会让指标悄悄变成另一件事。

真实事故：`_process_chunk` 只抄了 prompt/completion/total/cached 四个字段，
把 `completion_tokens_details` 整个丢了，于是 `reasoning_tokens` 在流式路径上
永远是 0。后果是 harbor 两臂对比里「思考到底开没开」验不出来，
一个假的「MyCode 快 1.9×」结论一路活到了汇报环节。
"""

from __future__ import annotations

from types import SimpleNamespace

from agents.core.model_caller import ModelCaller, _usage_details_for_trace


def _chunk(usage) -> SimpleNamespace:
    # choices 留空：_process_chunk 在处理完 usage 后就会 return，
    # 这样测试不需要伪造 agent 的文本/工具增量那一整套接口
    return SimpleNamespace(usage=usage, choices=[])


def _usage(**overrides) -> SimpleNamespace:
    base = dict(
        prompt_tokens=100,
        completion_tokens=40,
        total_tokens=140,
        prompt_tokens_details=None,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=25),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(chunk) -> dict:
    acc: dict = {"content": "", "tool_calls": {}, "finish_reason": "", "usage": None}
    ModelCaller(SimpleNamespace())._process_chunk(chunk, None, acc)
    return acc["usage"]


def test_reasoning_tokens_survive_the_stream_chunk():
    usage = _run(_chunk(_usage()))

    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 40
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 25


def test_missing_details_stays_none_instead_of_inventing_thinking():
    # 网关在关思考时整个 completion_tokens_details 都不返回；
    # 这时必须是 None（= 没有数据），不能凭空造一个 0 出来冒充「测到了 0」
    usage = _run(_chunk(_usage(completion_tokens_details=None)))

    assert usage["completion_tokens_details"] is None


def test_details_with_no_reasoning_field_counts_zero():
    usage = _run(_chunk(_usage(completion_tokens_details=SimpleNamespace())))

    assert usage["completion_tokens_details"]["reasoning_tokens"] == 0


def test_trace_details_split_output_into_reasoning_and_visible():
    details = _usage_details_for_trace(
        {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "total_tokens": 140,
            "cached_tokens": 10,
            "completion_tokens_details": {"reasoning_tokens": 25},
        }
    )

    # output 是含思考的合计值；拆开才看得出「慢是因为想得多」还是「因为写得多」
    assert details["output"] == 40
    assert details["output_reasoning_tokens"] == 25
    assert details["output_visible_tokens"] == 15
    assert details["input"] == 90
    assert details["input_cached_tokens"] == 10


def test_trace_details_without_any_details_key():
    details = _usage_details_for_trace(
        {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    )

    assert details["output_reasoning_tokens"] == 0
    assert details["output_visible_tokens"] == 40
