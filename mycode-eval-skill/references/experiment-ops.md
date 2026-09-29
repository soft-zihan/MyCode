# 无人值守实验运维

## Watcher 调度模式（小时级消融）

首臂串行测时长 → 按阈值决定余臂串行/并行。要点（全部来自真实事故）：

```bash
#!/bin/bash
# watcher.sh — nohup 起，配合 caffeinate 防休眠
cd <repo> && source .venv/bin/activate
touch /tmp/watcher.start
log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> /tmp/watcher.log; }

# 1) done 标记必须校验新鲜度（陈旧标记会让 watcher 秒退——v1 事故）
while true; do
  [ -f /tmp/arm1.done ] && [ /tmp/arm1.done -nt /tmp/watcher.start ] && break
  # 2) runner 死亡探测：进程没了又无新鲜 done → abort 留痕
  pgrep -qf "compression-arm full" || { [ -f /tmp/arm1.done ] && [ /tmp/arm1.done -nt /tmp/watcher.start ] || { log ABORT; touch /tmp/failed.done; exit 1; }; }
  sleep 60
done

# 3) 认证/熔断类错误 → abort 而非续跑（垃圾数据比没数据贵）
grep -lq "AuthenticationError\|circuit breaker" /tmp/arm1_*.log && { log ABORT-auth; touch /tmp/failed.done; exit 1; }

# 4) 首臂墙钟决定余臂调度
ELAPSED=$(( $(awk '/^END/{print $2}' /tmp/arm1.timing) - $(awk '/^START/{print $2}' /tmp/arm1.timing) ))
run_arm() { ...; touch /tmp/${1}.done; }
if [ "$ELAPSED" -gt 10800 ]; then
  for arm in none tool_only session_only; do run_arm $arm & done; wait   # 并行（session 隔离，可安全并发）
else
  for arm in none tool_only session_only; do run_arm $arm; done
fi
touch /tmp/all.done
```

配套：
- `nohup caffeinate -i -w <watcher_pid> &` — watcher 存活期间防空闲休眠
- 每臂独立 `START/END` epoch 写 timing 文件；日志分 seed 落盘
- 状态检查一条命令：`cat /tmp/watcher.log; ls /tmp/*.done`

## 长跑前置检查

- [ ] 网关直测目标模型 200（两次，排除瞬时抖动）：httpx POST chat/completions 最小请求
- [ ] 1 题 smoke 全链（请求指向 + meta + 触发 + 工作区位置）
- [ ] 磁盘：每题工作区可达数百 MB（LOCA/带附件 GAIA）
- [ ] 别在实验期间 `./start.sh restart`（backend_session 模式会杀活跃评测）；文档/前端改动无需重启
- [ ] Langfuse 导出超时（US cloud 网络）不阻塞 run，SDK 自动重试；不要因网络改方案

## 事后聚合

1. 报告归属：run_id 内嵌起始时间戳，对照 watcher timing 归臂（meta 已含 arm 后优先信 meta）
2. 配对分析：按 (seed, task_id) 建 pass 矩阵 → McNemar 错配对 + sign test
3. token 画像：report tokens 快速聚合 + Langfuse cache 明细（见 trace-tokens.md）
4. 机制核查后才写结论：触发计数、token 结构差异、失败形态分类三者与假设一致才可下结论
5. 结果书写成实验报告（结果表 + 解读 + 可复现命令）；无效实验同样要写清作废原因，禁止悄悄引用

## 常见故障速查

| 症状 | 根因模式 | 定位 |
|---|---|---|
| 全臂秒败 401 | 配置跨源混配 → 静默打 api.openai.com（BC-13） | 报错文案含 platform.openai.com 即 SDK 默认 base；网关直测排除 key 失效 |
| 0/N 完成 + 熔断 | 同上，连败触发 circuit breaker | 日志 `circuit breaker is open` |
| 四臂 token/pass 无结构差异 | 操纵变量未触发（BC-16） | grep should_compress / compression_events |
| agent 弃题引用"项目规则" | 工作区在仓库内继承 AGENTS.md（BC-17） | 事件日志看原话；查 workspace 路径 |
| 报告无法归因臂 | meta 缺 arm/window（BC-14） | 跑前查 meta |
| 仓库根冒任务产物 | in_process 无工作区隔离（BC-15） | git status；查 workspace 参数 |
| watcher 秒退 | 陈旧 done 标记 | mtime 新鲜度校验 |
