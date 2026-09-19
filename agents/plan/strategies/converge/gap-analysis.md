# Gap Analysis Converge

差距分析：对比 Spec 验收标准和实际实现，发现遗漏项。

## 流程

1. 所有任务完成后，读取 `{plan_dir}/spec.md` 的验收标准
2. 逐条检查验收标准是否满足
3. 分类 Finding：
   - **Missing**: 完全未实现
   - **Partial**: 部分实现
   - **Wrong**: 实现错误
   - **Extra**: 超出范围（非目标被实现）
4. 为每个 Finding 生成 convergence task
5. 追加到 `{plan_dir}/tasks.md`
6. 主 Agent 执行 convergence tasks
7. 最多 3 轮收敛

## 检查方法

1. 解析 spec.md 的验收标准（`## 验收标准` 下的 checkbox 列表）
2. 对每个标准：
   - 运行验收命令（如果有）
   - 检查相关代码是否存在
   - 检查功能是否正常
3. 检查非目标是否被违反

## Finding 格式

```json
{
    "id": "finding-1",
    "type": "Missing|Partial|Wrong|Extra",
    "severity": "high|medium|low",
    "criterion": "验收标准描述",
    "evidence": "检查结果描述",
    "suggested_task": "建议的修复任务描述"
}
```

## Convergence Task 格式

```markdown
### Task N: [Converge] 修复 {Finding ID}
- **类型**: {Finding type}
- **严重度**: {severity}
- **原始标准**: {criterion}
- **问题**: {evidence}
- **文件**: `path/to/file.py`
- **验收**: `pytest tests/test_file.py -v`
- **状态**: [ ] pending
```

## 注意事项

- 只检查 spec 中明确列出的验收标准
- Extra 类型的 Finding 需要人工确认是否删除
- 每轮收敛后重新检查，直到所有标准满足或达到 3 轮上限
- 超过 3 轮仍有问题，暂停并报告
