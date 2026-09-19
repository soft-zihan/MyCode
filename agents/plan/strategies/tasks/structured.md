# Structured Tasks

结构化任务格式，包含文件、函数、接口、验收等详细信息。

## 流程

1. 读取 `{plan_dir}/spec.md` 了解需求
2. 将需求拆分为独立的任务
3. 每个任务包含完整的上下文信息
4. 写入 `{plan_dir}/tasks.md`

## tasks.md 格式

```markdown
<!-- TASKS START -->
### Task 1: 任务标题
- **文件**: `path/to/file.py`
- **函数**: `function_name()`
- **接口**: `def function_name(param: str) -> bool`
- **依赖**: 无 / Task 1, Task 2
- **验收**: `pytest tests/test_file.py -v`
- **状态**: [ ] pending

### Task 2: 任务标题
- **文件**: `path/to/another.py`
- **函数**: `another_function()`
- **接口**: `def another_function() -> None`
- **依赖**: Task 1
- **验收**: `python -c "from module import func; assert func()"`
- **状态**: [ ] pending
<!-- TASKS END -->
```

## 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| 文件 | 是 | 要修改/创建的文件路径 |
| 函数 | 是 | 要实现的函数名 |
| 接口 | 是 | 函数签名 |
| 依赖 | 否 | 前置任务 ID 列表 |
| 验收 | 是 | 验收命令 |
| 状态 | 是 | pending/in-progress/done/failed |

## 注意事项

- 每个任务必须可独立验收
- 依赖关系要明确，避免循环依赖
- 验收命令必须是可执行的 shell 命令
