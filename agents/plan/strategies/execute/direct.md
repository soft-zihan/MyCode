# Direct Execute

主 Agent 直接执行任务，无额外编排逻辑。

## 流程

1. 读取 `{plan_dir}/tasks.md` 获取待执行任务
2. 按顺序逐个执行任务
3. 每个任务执行后调用 `plan_task_done` 或 `plan_task_failed`
4. 所有任务完成后调用 `plan_complete`

## 执行步骤

对于每个任务：

1. 调用 `plan_task_start(slug, task_id)` 标记开始
2. 读取任务定义，了解文件、接口、验收命令
3. 实现功能（写代码、创建文件等）
4. 运行验收命令
5. 如果通过，调用 `plan_task_done(slug, task_id, commit, verification)`
6. 如果失败，调用 `plan_task_failed(slug, task_id, error)`

## 注意事项

- 严格按照任务定义的接口实现
- 验收命令必须通过才能标记完成
- 失败时记录详细原因，方便后续重试
