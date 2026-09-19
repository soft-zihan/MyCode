# Single Axis Review

单轴审查：一个 Reviewer 子 Agent 检查代码质量和 Spec 合规性。

## 流程

1. 任务完成后，启动 Reviewer 子 Agent
2. Reviewer 检查：
   - Spec 合规：实现是否符合 spec 的需求和约束
   - 接口一致：函数签名是否与 task 定义一致
   - 验收通过：执行 task 定义中的验收命令
   - 代码质量：是否有明显 bug、遗漏、或 bad practice
3. 如果通过，标记任务完成
4. 如果不通过，生成 fix_list，主 Agent 修复后重新审查
5. 最多 3 轮审查，超过则暂停

## Reviewer 输入

- Spec 文档（`{plan_dir}/spec.md`）
- Task 定义（从 tasks.md 提取）
- 实现期间的代码变更

## Reviewer 输出格式

```json
{
    "passed": true/false,
    "fix_list": [
        {"file": "...", "line": N, "issue": "...", "suggestion": "..."}
    ],
    "verification": {
        "command": "...",
        "exit_code": N,
        "output_snippet": "..."
    },
    "summary": "一句话总结"
}
```

## Fix Loop

1. 主 Agent 收到 fix_list
2. 按顺序修复每个问题
3. 重新运行验收命令
4. 再次调用 Reviewer 审查
5. 重复直到通过或达到 3 轮上限

## 注意事项

- Reviewer 只能使用只读工具 + run_shell（执行验收命令）
- Reviewer 不能修改文件
- 每轮修复后必须重新验收
