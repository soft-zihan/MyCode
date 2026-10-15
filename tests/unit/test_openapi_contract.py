"""U12 契约漂移门禁：frontend/openapi.json 与 src/api/schema.d.ts 必须与再生成一致。

后端路由/pydantic 模型改动后未重新导出 → 此测试失败。修复：
    python frontend/server/export_openapi.py && (cd frontend && npm run gen:api)
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
FRONTEND = ROOT / "frontend"


def test_openapi_json_fresh(tmp_path):
    out = tmp_path / "openapi.json"
    subprocess.run(
        [sys.executable, str(FRONTEND / "server" / "export_openapi.py"), str(out)],
        check=True, capture_output=True,
    )
    committed = (FRONTEND / "openapi.json").read_text()
    assert out.read_text() == committed, (
        "openapi.json 契约漂移：运行 python frontend/server/export_openapi.py "
        "&& cd frontend && npm run gen:api 并提交生成物"
    )


def test_schema_dts_fresh(tmp_path):
    binary = FRONTEND / "node_modules" / ".bin" / "openapi-typescript"
    if not binary.exists():
        # 这条要跑 openapi-typescript，所以它需要 node 环境。CI 的 backend 作业
        # 从不装 node（只有 frontend 作业装），于是它在那里**永远不可能通过**——
        # 而一个恒 fail 的门禁等于没有门禁，还会把真正的漂移淹在噪声里。
        # 门禁本体已经搬到 CI 的 frontend 作业（"Contract freshness" 步骤：
        # npm run gen:api 后 git diff --exit-code），那里有 node。
        # 本地装了 node_modules 时这条照常严格校验，所以能力没有丢。
        pytest.skip("frontend/node_modules 未安装；契约新鲜度由 CI frontend 作业把关")
    out = tmp_path / "schema.d.ts"
    subprocess.run(
        [str(binary), str(FRONTEND / "openapi.json"), "-o", str(out)],
        check=True, capture_output=True, cwd=FRONTEND,
    )
    committed = (FRONTEND / "src" / "api" / "schema.d.ts").read_text()
    assert out.read_text() == committed, (
        "schema.d.ts 漂移：cd frontend && npm run gen:api 并提交生成物"
    )
