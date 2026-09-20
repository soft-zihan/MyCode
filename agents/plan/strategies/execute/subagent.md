# Subagent Execute

子 Agent 隔离执行：每个任务派发一个 subagent 在独立上下文中完成，主 Agent 只负责编排与验收。适合任务之间上下文耦合低、单任务上下文消耗大的计划。

## 流程

对 `{slug}` 的每个待执行任务：

1. 调用 `plan_task_start(slug, task_id)` 标记开始
2. 用 `agent` 工具派发子 Agent，任务提示中必须包含：
   - 任务描述、目标文件、接口签名（从 tasks.md 该任务块提取）
   - 验收命令（要求子 Agent 运行并报告 exit_code 与输出摘要）
   - 明确边界：只做本任务范围内的事，不做无关重构
3. 子 Agent 返回后，主 Agent 审查其产出：
   - 运行验收命令复核（不轻信子 Agent 自报结果）
   - 检查改动是否越界（git diff）
4. 复核通过 → `plan_task_done(slug, task_id, commit, verification)`，verification 填主 Agent 复核的命令与结果
5. 复核失败 → 派发修复子 Agent（附上失败证据），最多 2 轮；仍失败 → `plan_task_failed(slug, task_id, error)`
6. 所有任务完成后调用 `plan_complete(slug)`

## 注意事项

- 一次只派发一个任务，禁止并行派发有依赖关系的任务
- 子 Agent 上下文是隔离的：任务提示必须自包含，不能引用"上面的讨论"
- 主 Agent 保留编排上下文，避免被实现细节污染
- verification 必须来自主 Agent 亲自运行的验收命令
