"""可执行 Skills（Executable Skills）加载器。

Skills 从纯 Markdown 指令升级为可执行 Python 包：
- skill 目录包含 pyproject.toml + src/<name>/__init__.py
- 运行时自动安装到隔离 venv 并注入 Agent 命名空间
- 模型可直接 await skill_name(...) 调用

设计参考：Prime Agent 的 Python-backed skills。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from agents.observability.trace import trace_span


# ── 配置 ───────────────────────────────────────────────────────────────────────


def get_kernel_venv_path() -> Path:
    """获取 skill 隔离 venv 路径。"""
    override = os.environ.get("MYCODE_SKILL_VENV", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".my-code" / "skill-venv"


def get_python_executable() -> str:
    """获取 venv 中的 Python 可执行文件路径。"""
    venv = get_kernel_venv_path()
    if sys.platform == "win32":
        return str(venv / "Scripts" / "python.exe")
    return str(venv / "bin" / "python")


def get_pip_executable() -> str:
    """获取 venv 中的 pip 可执行文件路径。"""
    venv = get_kernel_venv_path()
    if sys.platform == "win32":
        return str(venv / "Scripts" / "pip.exe")
    return str(venv / "bin" / "pip")


# ── 检测 ───────────────────────────────────────────────────────────────────────


def is_executable_skill(skill_dir: Path) -> bool:
    """检测 skill 目录是否为可执行 skill（包含 pyproject.toml）。"""
    if not skill_dir.is_dir():
        return False
    pyproject = skill_dir / "pyproject.toml"
    if not pyproject.is_file():
        return False
    # 检查 src/<import_name>/__init__.py
    skill_name = skill_dir.name
    import_name = skill_name.replace("-", "_")
    init_file = skill_dir / "src" / import_name / "__init__.py"
    return init_file.is_file()


def get_import_name(skill_name: str) -> str:
    """将 skill 名称转换为 Python import 名称。"""
    return skill_name.replace("-", "_")


# ── 安装 ───────────────────────────────────────────────────────────────────────


def ensure_venv() -> bool:
    """确保 venv 存在，不存在则创建。"""
    venv_path = get_kernel_venv_path()
    python_exe = get_python_executable()

    if venv_path.is_dir() and Path(python_exe).is_file():
        return True

    with trace_span(
        "skill.venv",
        input=str(venv_path),
        metadata={"path": str(venv_path)},
    ) as span:
        try:
            venv_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                check=True,
                capture_output=True,
            )
            span.add_metadata(success=True)
            return True
        except subprocess.CalledProcessError as e:
            span.update(output=str(e)[:2000], metadata={"success": False})
            span.record_error(e)
            return False


def install_skill_package(skill_dir: Path) -> dict[str, Any]:
    """将 skill 包安装到 venv（editable 模式）。

    Returns:
        {"success": bool, "import_name": str, "error": str | None}
    """
    result: dict[str, Any] = {
        "success": False,
        "import_name": "",
        "error": None,
    }

    if not ensure_venv():
        result["error"] = "Failed to create venv"
        return result

    skill_name = skill_dir.name
    import_name = get_import_name(skill_name)
    result["import_name"] = import_name

    pip_exe = get_pip_executable()

    with trace_span(
        "skill.install",
        input=str(skill_dir),
        metadata={
            "skill_name": skill_name,
            "import_name": import_name,
            "path": str(skill_dir),
        },
    ) as span:
        try:
            subprocess.run(
                [pip_exe, "install", "-e", str(skill_dir)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            result["success"] = True
            span.add_metadata(success=True)
        except subprocess.CalledProcessError as e:
            result["error"] = f"pip install failed: {e.stderr.decode()[:500]}"
            span.update(output=result["error"][:2000], metadata={"success": False})
            span.record_error(e)
        except subprocess.TimeoutExpired as e:
            result["error"] = "pip install timed out"
            span.update(output=result["error"], metadata={"success": False})
            span.record_error(e)

    return result


def rebuild_venv_if_needed() -> bool:
    """如果 pyproject.toml 变化，重建 venv。"""
    # 简化实现：检查 venv 是否存在
    venv_path = get_kernel_venv_path()
    if not venv_path.is_dir():
        return ensure_venv()
    return True


# ── 注入 ───────────────────────────────────────────────────────────────────────


def inject_skill_to_namespace(
    import_name: str,
    namespace: dict[str, Any],
) -> dict[str, Any]:
    """将 skill 注入到 Agent 命名空间。

    如果模块定义了 run()，则注入 run 函数；
    否则注入整个模块。

    Returns:
        {"success": bool, "callable": Callable | None, "error": str | None}
    """
    result: dict[str, Any] = {
        "success": False,
        "callable": None,
        "error": None,
    }

    with trace_span(
        "skill.inject",
        input=import_name,
        metadata={"import_name": import_name},
    ) as span:
        try:
            mod = importlib.import_module(import_name)

            if hasattr(mod, "run"):
                namespace[import_name] = mod.run
                result["callable"] = mod.run
            else:
                namespace[import_name] = mod
                result["callable"] = mod

            result["success"] = True
            span.add_metadata(success=True, has_run=hasattr(mod, "run"))

        except ImportError as e:
            result["error"] = f"Import failed: {e}"
            span.update(output=result["error"][:2000], metadata={"success": False})
            span.record_error(e)
        except Exception as e:
            result["error"] = f"Unexpected error: {e}"
            span.update(output=result["error"][:2000], metadata={"success": False})
            span.record_error(e)

    return result


# ── 高级 API ───────────────────────────────────────────────────────────────────


class ExecutableSkillLoader:
    """可执行 Skill 加载器。

    管理 skill 的安装、注入和生命周期。
    """

    def __init__(self):
        self._installed: dict[str, Path] = {}  # import_name -> skill_dir
        self._namespace: dict[str, Any] = {}

    def load_skill(self, skill_dir: Path) -> dict[str, Any]:
        """加载单个 skill（安装 + 注入）。"""
        if not is_executable_skill(skill_dir):
            return {
                "success": False,
                "error": "Not an executable skill (missing pyproject.toml)",
            }

        skill_name = skill_dir.name
        import_name = get_import_name(skill_name)

        # 检查是否已加载
        if import_name in self._installed:
            return {
                "success": True,
                "import_name": import_name,
                "already_loaded": True,
            }

        # 安装
        install_result = install_skill_package(skill_dir)
        if not install_result["success"]:
            return install_result

        # 注入
        inject_result = inject_skill_to_namespace(import_name, self._namespace)
        if not inject_result["success"]:
            return inject_result

        self._installed[import_name] = skill_dir
        return {
            "success": True,
            "import_name": import_name,
            "callable": inject_result["callable"],
        }

    def load_all_skills(self, skill_dirs: list[Path]) -> dict[str, Any]:
        """批量加载 skills。"""
        results: list[dict[str, Any]] = []
        for skill_dir in skill_dirs:
            if is_executable_skill(skill_dir):
                result = self.load_skill(skill_dir)
                results.append({
                    "skill_dir": str(skill_dir),
                    **result,
                })

        return {
            "total": len(skill_dirs),
            "executable": len(results),
            "loaded": sum(1 for r in results if r.get("success")),
            "failed": sum(1 for r in results if not r.get("success")),
            "results": results,
        }

    def get_namespace(self) -> dict[str, Any]:
        """获取当前命名空间（包含所有已加载的 skills）。"""
        return dict(self._namespace)

    def get_callable(self, import_name: str) -> Optional[Callable]:
        """获取已加载 skill 的可调用对象。"""
        return self._namespace.get(import_name)

    def is_loaded(self, import_name: str) -> bool:
        """检查 skill 是否已加载。"""
        return import_name in self._installed

    def list_loaded(self) -> list[str]:
        """列出所有已加载的 skill import names。"""
        return list(self._installed.keys())


# ── 全局实例 ───────────────────────────────────────────────────────────────────


_loader: Optional[ExecutableSkillLoader] = None


def get_executable_skill_loader() -> ExecutableSkillLoader:
    """获取全局 ExecutableSkillLoader 实例。"""
    global _loader
    if _loader is None:
        _loader = ExecutableSkillLoader()
    return _loader


def load_executable_skills(skill_dirs: list[Path]) -> dict[str, Any]:
    """加载所有可执行 skills 的便捷函数。"""
    loader = get_executable_skill_loader()
    return loader.load_all_skills(skill_dirs)


def get_executable_skill_namespace() -> dict[str, Any]:
    """获取可执行 skill 命名空间的便捷函数。"""
    loader = get_executable_skill_loader()
    return loader.get_namespace()
