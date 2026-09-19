# Vertical Slice Tasks

垂直切片任务格式，每个任务是一个完整的功能切片，包含前后端。

## 流程

1. 读取 `{plan_dir}/spec.md` 了解需求
2. 识别功能切片（每个切片是一个用户可感知的功能）
3. 每个切片包含完整的前后端实现
4. 使用 Blocking 关系管理依赖
5. 写入 `{plan_dir}/tasks.md`

## tasks.md 格式

```markdown
<!-- TASKS START -->
### Task 1: 用户注册 API
- **切片**: 用户认证
- **文件**: `src/auth/register.py`, `tests/test_register.py`
- **接口**: `POST /api/register`
- **验收**: `pytest tests/test_register.py -v && curl -X POST localhost:8000/api/register -d '{"email":"test@test.com"}'`
- **Blocked by**: 无
- **状态**: [ ] pending

### Task 2: 用户注册 UI
- **切片**: 用户认证
- **文件**: `src/components/RegisterForm.tsx`, `src/pages/register.tsx`
- **接口**: `RegisterForm` 组件
- **验收**: `playwright test tests/e2e/register.spec.ts`
- **Blocked by**: Task 1
- **状态**: [ ] pending
<!-- TASKS END -->
```

## 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| 切片 | 是 | 所属功能切片名称 |
| 文件 | 是 | 要修改/创建的文件路径（可多个） |
| 接口 | 是 | API 端点或组件接口 |
| 验收 | 是 | 验收命令（可多个） |
| Blocked by | 是 | 前置任务 ID 列表（无则填"无"） |
| 状态 | 是 | pending/in-progress/done/failed |

## 垂直切片原则

1. **端到端**: 每个切片从数据库到 UI 完整实现
2. **可独立演示**: 每个切片完成后可以独立演示
3. **小步交付**: 优先交付核心切片，再补充边缘功能

## Blocking 关系

- 使用 `Blocked by` 字段声明依赖
- 只有所有前置任务完成，当前任务才能开始
- 避免循环依赖

## 注意事项

- 切片粒度适中，每个切片 1-2 小时可完成
- 优先实现核心功能的切片
- 同一切片内的任务尽量连续完成
