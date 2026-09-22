"""LOCA-bench 集成：MyCode 整只 agent 作为 scaffold 的可控上下文增长评测。

架构（双 venv 进程隔离，两个项目互不侵入）：
- runner.py    (本项目 venv) 编排器：选题、起 loca_side 子进程、聚合报告
- loca_side.py (LOCA venv)   env 生命周期 + MCP stdio 配置生成 + claim_done 评分
- agent_main.py(本项目 venv) 单任务 MyCode Agent 执行器（经 run_agent_task，带 trace）

协议：文件交换 loca_task.json / agent_result.json / loca_eval.json；
MCP 服务器以 stdio 子进程跨界（命令在 LOCA 侧绝对化为 LOCA venv 解释器）。
"""
