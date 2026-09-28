"""U12 契约漂移门禁：frontend/openapi.json 与 src/api/schema.d.ts 必须与再生成一致。

后端路由/pydantic 模型改动后未重新导出 → 此测试失败。修复：
    python frontend/server/export_openapi.py && (cd frontend && npm run gen:api)
"""
import subprocess
import sys
from pathlib import Path

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
    assert binary.exists(), "frontend/node_modules 未安装：cd frontend && npm install"
    out = tmp_path / "schema.d.ts"
    subprocess.run(
        [str(binary), str(FRONTEND / "openapi.json"), "-o", str(out)],
        check=True, capture_output=True, cwd=FRONTEND,
    )
    committed = (FRONTEND / "src" / "api" / "schema.d.ts").read_text()
    assert out.read_text() == committed, (
        "schema.d.ts 漂移：cd frontend && npm run gen:api 并提交生成物"
    )
