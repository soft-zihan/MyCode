# Dual Axis Review

双轴并行审查：两个 Reviewer 子 Agent 分别从不同角度审查代码。

## 审查轴

### 轴 1: Standards Reviewer
检查代码质量和最佳实践：
- 代码风格一致性
- 命名规范
- 错误处理
- 性能问题
- 安全漏洞

### 轴 2: Spec Reviewer
检查 Spec 合规性：
- 需求是否完整实现
- 验收标准是否满足
- 约束是否遵守
- 非目标是否被违反

## 流程

1. 任务完成后，同时启动两个 Reviewer 子 Agent
2. 两个 Reviewer 并行审查
3. 收集两个审查结果
4. 合并 fix_list（去重）
5. 如果两个都通过，标记任务完成
6. 如果任一不通过，生成合并的 fix_list，主 Agent 修复
7. 最多 3 轮审查

## Reviewer 输出格式

```json
{
    "axis": "standards" | "spec",
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

## 合并规则

1. 两个轴都通过 → 任务通过
2. 一个通过一个不通过 → 任务不通过，使用不通过的 fix_list
3. 两个都不通过 → 任务不通过，合并两个 fix_list（按文件+行号去重）

## 注意事项

- 两个 Reviewer 使用不同的系统提示词
- Standards Reviewer 使用 Fowler 的代码异味清单作为参考
- Spec Reviewer 专注于需求合规性
- 并行执行可以节省时间
