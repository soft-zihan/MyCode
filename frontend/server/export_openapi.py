#!/usr/bin/env python3
"""U12：导出 OpenAPI 契约单源 → frontend/openapi.json。

后端路由/pydantic 模型变更后运行：
    python frontend/server/export_openapi.py && (cd frontend && npm run gen:api)
漂移由 tests/unit/test_openapi_contract.py 门禁（生成物必须与提交物一致）。
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))                    # main.py 的 `from routers import ...`
sys.path.insert(0, str(HERE.parent.parent))      # 仓库根：`frontend.server.*` 包路径


def build_spec() -> dict:
    from main import app
    return app.openapi()


def main(out_path: Path | None = None) -> None:
    spec = build_spec()
    out = out_path or (HERE.parent / "openapi.json")
    # sort_keys：确定性输出，diff 即契约漂移
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"[openapi] exported {len(spec.get('paths', {}))} paths -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
