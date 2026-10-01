"""U5a-5：工具描述动态化——agent 工具注入子代理清单、run_shell 注入 shell/OS。

v2 "description 即动态 prompt" 模式（subagent.ts:271-295、shell.ts:275-282）：
清单/环境信息注入工具描述（每请求重解析），不占用冻结的 system prompt。
"""
from agents.core.model_caller import _to_openai_tools
from agents.tools.registry import tool_definitions


def _defs(names):
    return [t for t in tool_definitions if t["name"] in names]


def test_agent_description_lists_available_types():
    out = _to_openai_tools(_defs({"agent"}))
    desc = out[0]["function"]["description"]
    assert "Available agent types:" in desc
    for name in ("explore", "reviewer", "general"):
        assert f"'{name}'" in desc
    # D7：enum 与清单同步重解析
    schema = out[0]["function"]["parameters"]
    assert set(schema["properties"]["type"]["enum"]) >= {"explore", "reviewer", "general"}


def test_agent_description_includes_custom_agents(monkeypatch):
    import agents.core.model_caller as model_caller

    # 消费方绑定（模块级 import）——补丁必须打在 model_caller 命名空间
    monkeypatch.setattr(model_caller, "get_available_agent_types", lambda: [
        {"name": "explore", "description": "e"},
        {"name": "custom-x", "description": "does X"},
    ])
    out = _to_openai_tools(_defs({"agent"}))
    fn = out[0]["function"]
    assert "custom-x" in fn["description"]
    assert "does X" in fn["description"]
    assert fn["parameters"]["properties"]["type"]["enum"] == ["explore", "custom-x"]


def test_run_shell_description_has_environment():
    out = _to_openai_tools(_defs({"run_shell"}))
    desc = out[0]["function"]["description"]
    assert "Environment:" in desc
    assert "commands run via" in desc


def test_other_tools_description_untouched():
    out = _to_openai_tools(_defs({"read_file"}))
    src = next(t for t in tool_definitions if t["name"] == "read_file")
    assert out[0]["function"]["description"] == src["description"]


def test_system_prompt_no_agents_section():
    """{{agents}} 占位符与 Custom Agent Types 段已从 system prompt 移除。"""
    from agents.core.prompt import build_system_prompt, _load_system_prompt_template

    assert "{{agents}}" not in _load_system_prompt_template()
    prompt = build_system_prompt()
    assert "Custom Agent Types" not in prompt


def test_tool_guidance_conditional_on_tools():
    """U5a-6：工具指引按工具存在性条件生成（v2 OPENCODE_TOOL_GUIDANCE 模式）。"""
    from agents.core.prompt import build_tool_guidance

    full = build_tool_guidance(None)
    assert "# 使用工具" in full
    assert "run_shell" in full
    assert "mcp__code-review-graph__query_graph_tool" in full
    assert "`agent` 工具调用专门的" in full

    # 有 shell + 部分专用工具：只注入持有工具的纪律行
    restricted = build_tool_guidance({"run_shell", "read_file", "grep_search"})
    assert "读取文件使用 read_file" in restricted
    assert "搜索文件内容使用 grep_search" in restricted
    assert "edit_file" not in restricted          # 未持有 edit_file → 该行不注入
    assert "web_search" not in restricted         # 未持有 web_search → 该行不注入
    assert "mcp__code-review-graph__" not in restricted  # 无代码图 → 无图指引
    assert "`agent` 工具调用专门的" not in restricted      # 无 agent 工具 → 无子代理段
    assert "并行执行所有独立的工具调用" in restricted        # 通用段恒在

    # 无 shell → 专用工具纪律整段不需要存在
    no_shell = build_tool_guidance({"read_file"})
    assert "run_shell" not in no_shell
    assert "读取文件使用 read_file" not in no_shell

    with_graph = build_tool_guidance({"run_shell", "mcp__code-review-graph__query_graph_tool"})
    assert "mcp__code-review-graph__query_graph_tool" in with_graph


def test_system_prompt_renders_tool_guidance():
    from agents.core.prompt import build_system_prompt

    prompt = build_system_prompt(active_tools={"read_file"})
    assert "{{tool_guidance}}" not in prompt
    assert "{{deferred_tools}}" not in prompt
    assert "# 使用工具" in prompt
    restricted = build_system_prompt(active_tools={"read_file"})
    assert "mcp__code-review-graph__" not in restricted


def test_tool_guidance_empty_and_interaction_rules():
    """U5b-#5：无工具时空段；工具交互通则从内核下沉且随工具存在注入。"""
    from agents.core.prompt import build_tool_guidance, _load_system_prompt_template

    assert build_tool_guidance(set()) == ""
    full = build_tool_guidance(None)
    for marker in ("<tool_result>", "system-reminder", "hooks", "compact_context", "冒号"):
        assert marker in full
    # compact_context 指引以该工具存在为前提
    assert "compact_context" not in build_tool_guidance({"read_file"})
    # 内核不再包含工具交互细节与 CLI REPL 帮助
    tpl = _load_system_prompt_template()
    for removed in ("<tool_result>", "system-reminder", "REPL 命令"):
        assert removed not in tpl


def test_task_list_guidance_present_with_tool():
    """Smoke fix：持有 task_list 时注入实质性任务管理指引。

    断言钉的是行为性内容（触发阈值 / 反过度工程条款 / 不轮询 list /
    detail 与 acceptance 字段写法 / 状态纪律 / after_id / 自动披露），
    只提一句工具名的水化实现过不了。
    """
    from agents.core.prompt import build_tool_guidance

    g = build_tool_guidance({"task_list", "read_file"})
    # 触发条件具体化：3 步阈值、多个待办、跨多文件，且在动手之前建清单
    assert "3 个以上独立步骤" in g
    assert "跨多个文件" in g
    assert "动手之前" in g
    assert "不是可选仪式" in g
    # 反过度工程条款：建清单/维护清单是任务执行的一部分
    assert "任务执行的一部分" in g
    # 摘要常驻上下文尾部 → 禁止轮询 list；get 只用于按需取单条完整方案
    assert "不要调用 list 检查进度" in g
    assert "get 仅用于按需取单条任务的完整方案" in g
    # 字段写法：acceptance 是一条可验证命令；detail 按读者无其他上下文写
    assert "一条能证明完成的可验证命令" in g
    assert "按读者没有任何其他上下文来写" in g
    # 状态纪律：单一 in_progress、failed 填 error
    assert "同一时间只有一条 in_progress" in g
    assert "failed 并填写 error" in g
    # 清单可编辑（after_id）+ detail 自动注入（不需要主动拉）
    assert "after_id" in g
    assert "自动注入" in g


def test_task_list_guidance_absent_without_tool():
    """Smoke fix：未持有 task_list（子代理场景）→ 整段不注入。

    task_list 在 subagent 的 _sub_agent_excluded 里，若指引下沉到内核
    会告诉子代理去用一个它没有的工具。
    """
    from agents.core.prompt import build_tool_guidance

    g = build_tool_guidance({"read_file"})
    assert "task_list" not in g
    assert "3 个以上独立步骤" not in g
    assert "in_progress" not in g
    assert "以 `…` 结尾" not in g          # 截断纪律随 task_list 一起不注入


def test_task_list_guidance_warns_about_truncated_acceptance():
    """常驻摘要 S 里带 `…` 的 acceptance 是**半条命令**，指引必须禁止直接执行。

    验收闸门叫模型去执行它在 S 里读到的那条 acceptance；`…` 让截断可见，但可见
    不等于可执行——模型仍可能跑那条前缀，而 run_shell 成功了闸门就不响。所以指引
    要给出出路（get 取全文）与禁令（不要跑前缀）。
    """
    from agents.core.prompt import build_tool_guidance

    g = build_tool_guidance({"task_list", "read_file"})
    assert "…" in g                       # 标记本身在指引里被点名
    assert "以 `…` 结尾" in g
    assert "get 取完整命令" in g            # 出路：拉全文
    assert "不要跑这条前缀" in g            # 禁令：截断前缀不可执行


def test_task_list_guidance_present_when_all_tools():
    """Smoke fix：active_tools=None（全量注入语义）→ 任务管理指引在场。"""
    from agents.core.prompt import build_tool_guidance

    g = build_tool_guidance(None)
    assert "3 个以上独立步骤" in g
    assert "不要调用 list 检查进度" in g
    assert "一条能证明完成的可验证命令" in g
