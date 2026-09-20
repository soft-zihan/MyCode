# TDD Execute

测试驱动执行：每个任务先写失败测试，再实现使其通过。适合逻辑密集、回归风险高的计划。

## 流程

对 `{slug}` 的每个待执行任务：

1. 调用 `plan_task_start(slug, task_id)` 标记开始
2. **Red**：根据任务的验收标准先编写测试用例，运行并确认失败（失败原因必须是"功能未实现"而非测试本身错误）
3. **Green**：实现最小可用代码使测试通过，运行验收命令确认通过
4. **Refactor**：在测试保护下消除重复、改善命名，重跑测试确认仍通过
5. 调用 `plan_task_done(slug, task_id, commit, verification)`：
   - verification.command 填最终测试命令（如 `pytest tests/test_x.py -v`）
   - verification.exit_code 必须为 0
   - commit 建议将测试与实现放在同一提交
6. 失败且无法修复 → `plan_task_failed(slug, task_id, error)`，error 中保留失败测试输出摘要
7. 所有任务完成后调用 `plan_complete(slug)`

## 注意事项

- 禁止先写实现再补测试（失去 TDD 意义）
- 测试文件放在项目既有测试目录，遵循项目测试框架与命名约定
- 每步都要真实运行命令，verification 不接受推测结果
- 任务粒度过大时（无法用单一测试刻画），先用 `plan_add_artifact` 记录拆分建议再执行
