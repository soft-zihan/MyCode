# 实验设计清单（消融/对比实验）

## 设计阶段检查单

开跑前逐项确认，任何一项不满足就先修再跑：

1. **操纵变量会触发吗？**
   - 换算触发线：`effective = window - 20K`；工具折叠触发 = 估算 tokens > 80% × effective
   - 用历史数据或 1-2 题 smoke 估任务上下文峰值，峰值必须越过触发线
   - smoke 断言：runner 输出的 `compression_events`（tool_folded/session_folded/events_hidden 计数）> 0
   - 反例（BC-16）：GAIA L3 峰值 76K vs 980K 窗触发线 784K → 80 runs 全部白跑
2. **题集在模型能力区间吗？**
   - 目标 pass 率 20%-70%；先 3 题探测（小模型/新模型必做）
   - 全 0 → 降难度（L3→L2→L1）或换题集；全 1 → 升难度或加大压力维度
3. **配对与全量口径**
   - 正式实验**全量跑完整个题集 / config set，不抽样**；`--sample N` 只用于探测与校准
   - 同题集跨臂；若做双 seed 复现，臂间 seed 必须一致
   - 统计：McNemar 错配对计数（a 独赢 vs b 独赢）+ sign test 单边 p；不引用裸点估计差
   - 样本量不足时只有配对方向性结论可用，须在报告中显式标注推断力限制
4. **控制变量**
   - 同模型、同 thinking 状态（思考模型评测默认 `--thinking off` 提速）、同采样参数
   - 记录臂的起止时间（网络/网关状况随时段变化，是混杂因子）
5. **基础设施预检**（每项都是真实事故）
   - [ ] 1 题 smoke：请求指向正确（401/限流 → 查 resolve_model_config 成对解析，BC-13）
   - [ ] 报告 meta 含全部实验变量（arm/window/thinking/model/seed/level，BC-14）
   - [ ] 工作区在 `~/.mycode/eval_workspaces/`（仓库外；产物隔离 + 规则隔离，BC-15/17）
   - [ ] 评测 agent 输出无 "According to the project rules" 类引用（规则污染信号）
6. **结果解读纪律**
   - 异常漂亮/异常符合预期 → 先机制核查（触发计数、token 画像是否与机制一致）再下结论
   - token 佐证：压缩臂与不压缩臂累计输入应有结构性差异；四臂 token 几乎相同 = 机制未分化
   - 无效实验必须显式作废（文档撤回 + BC 记录），禁止悄悄引用

## 压缩消融变量矩阵

| 维度 | 取值 | 说明 |
|---|---|---|
| arm | full / truncate / tool_only / session_only | truncate=朴素硬截断（同触发点/同水位/无摘要/保首条指令/整组隐藏）；原 none 臂已删 |
| window | 100k / 262k / 1000k / 任意整数 | 压力窗口维度；100k → 触发线 64K |
| level (GAIA) | 1 / 2 / 3 / all | 默认 3（对比实验口径）；小模型用 2 或 1 |
| model | 端点任意模型 | 小模型（如 qwen3.6-27b）提速；端点 context_window 要如实填模型真实上限 |
| thinking | on / off / default | 评测默认 off |
| config-set (LOCA) | 8k…256k | 环境描述长度；与窗口搭配控制触发 |

## 基准数据锚点（避免重复踩坑）

- GAIA 题量：L1=53 / L2=86 / L3=26（媒体过滤后 L3=22）；L3 任务上下文峰值 ~76K、
  题均 ~20 次 model 调用、题均累计输入 ~45 万 token、cache 率 ~86%
- LOCA：每 config set 75 配置 = 15 任务 × 5 states（task-major 连续排列；全量跑即覆盖
  全部 15 任务，抽样时 head-N 只覆盖 ⌈N/5⌉ 个任务，任务多样性不足）；claim_done 评分
  reward∈[0,1] 但实测近二值，失败题 step_info.error 可解析 match-rate 作连续代理
  （仅部分失败类型含该字段）
- 校准数据（qwen3.6-27b thinking-off, 2026-09-22）：GAIA L2@100k=70%/0 触发（峰值
  77.7%）、L2@48k=60%/7 触发、L3=33% 且单题最长 26min 不适合小模型；LOCA
  128k@100k=33% pass、2/3 题触发——LOCA 是压缩消融主战场（可控增长 + 无
  web_search 环境混杂 + 确定性）
- 单题耗时：LOCA 128k ≈ 8.5min（qwen3.6-27b）；GAIA L2 ≈ 2-5min
- 内存预算（16GB 机器）：每并发任务 = agent_main + loca_side 双 Python + 可能的
  chromium 树 ≈ 1-1.5GB → 评测并发上限 4-6；API 侧允许高并发 ≠ 本地扛得住
  （BC-18 OOM 事故：16 并发 + 孤儿 chromium 泄漏 → 整机重启）
