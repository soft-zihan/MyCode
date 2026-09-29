---
name: visualization
description: 生成图表图片（折线/柱状/散点/热力图、mermaid 流程图/时序图/架构图、graphviz 依赖图、截图标注）并输出到 artifacts 通道，在回复中内联显示。
when-to-use: 用户要求画图/可视化/出图，或回复涉及数据对比、趋势、流程、时序、架构、依赖关系且图表比纯文字更清晰时。
user-invocable: false
---

# 可视化出图

你的目标：**产出图片文件并以内联 Markdown 图片展示在回复里**，而不是用文字描述图。

## 何时出图

优先出图的内容形态：
- 数据对比 / 趋势 / 分布 → matplotlib（折线、柱状、散点、热力）
- 流程 / 时序 / 架构 / 状态机 → mermaid
- 模块依赖 / 调用关系图 → mermaid 或 graphviz
- 已有截图上标框、标注 → PIL 标注脚本

不要为简单一次性回答出图；一图一信息，避免装饰性图表。

## 输出到哪（artifacts 通道）

1. 所有图片写入 artifacts 目录（先 `mkdir -p`）：`${ARTIFACTS_DIR}`
2. 文件名语义化 + 唯一（如 `dep-graph-auth.png`）
3. 生成成功后，在回复中用**相对 URL** 内联展示（前端会话内渲染 + 刷新后仍在）：

```
![模块依赖图](/api/artifacts/${SESSION_ID}/dep-graph-auth.png)
```

当前会话 ID：`${SESSION_ID}`

## 怎么调用

脚本目录：`${MYCODE_SKILL_DIR}/scripts/`。用 run_shell 执行（项目有 .venv 时用 `.venv/bin/python`，否则 `python3`）。所有脚本成功时打印 `OUT=<绝对路径>`。

### 1. matplotlib 图表（line / bar / scatter / heatmap）

```bash
python <scripts>/matplotlib_chart.py --type bar --data chart.json --out <artifacts>/compare.png --title "耗时对比" --xlabel 方案 --ylabel ms
```

`chart.json` 格式：
- line/bar：`{"labels": ["a","b"], "series": [{"name": "s1", "values": [1,2]}]}`（单序列可省 name）
- scatter：`{"series": [{"name": "s1", "values": [[x,y],[x,y]]}]}`
- heatmap：`{"matrix": [[1,2],[3,4]], "x_labels": ["c1","c2"], "y_labels": ["r1","r2"]}`

中文标签自动使用系统 CJK 字体。依赖缺失时脚本会打印安装命令（如 `.venv/bin/pip install matplotlib`），执行后重试。

### 2. mermaid（流程图 / 时序图 / 架构图 / 依赖图）

```bash
python <scripts>/mermaid_render.py --code 'graph TD; A-->B; B-->C;' --out <artifacts>/flow.png
```

也可 `--mmd diagram.mmd` 传文件。依赖 `mmdc`（@mermaid-js/mermaid-cli）；缺失时脚本给出安装提示。渲染失败时把 mermaid 源码原样放进代码块作为降级展示，并告知用户原因。

### 3. graphviz（大规模依赖图更清晰）

```bash
python <scripts>/graphviz_render.py --code 'digraph {a->b; b->c;}' --out <artifacts>/deps.png
```

依赖系统 `dot`（graphviz）；缺失时优先降级用 mermaid 画。

### 4. 截图标注（PIL）

```bash
python <scripts>/annotate_image.py --image shot.png --annotations '[{"box":[100,80,300,200],"label":"问题区域","color":"red"}]' --out <artifacts>/shot-annotated.png
```

## 流程纪律

1. 先想清楚图型与数据，再一次性生成（不要反复试错刷屏）
2. 生成后**核验文件存在**（脚本打印 OUT= 即成功）
3. 回复 = 一句话结论 + 内联图片 Markdown；不要把图片路径当纯文本贴出
4. 需要沉淀为项目知识的图（如架构图），可再用 remember 工具把结论与图片路径写入 wiki
