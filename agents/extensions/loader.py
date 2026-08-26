"""Extension loader and discovery.

扩展目录结构:
~/.bear/extensions/
├── my_extension.py
├── another_ext/
│   ├── __init__.py
│   └── ext.py
└── ...

每个扩展必须定义 setup(ext: ExtensionAPI) 函数。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .event_bus import EventBus, EventType
from .api import ExtensionAPI


DEFAULT_EXTENSIONS_DIR = Path.home() / ".bear" / "extensions"


@dataclass
class Extension:
    """扩展定义。"""
    name: str
    path: Path
    module: Any = None
    loaded_at: float = field(default_factory=time.time)
    enabled: bool = True
    error: str = ""

    @property
    def has_setup(self) -> bool:
        return self.module is not None and hasattr(self.module, "setup")


class ExtensionLoader:
    """扩展加载器。"""

    def __init__(
        self,
        extensions_dir: Path = DEFAULT_EXTENSIONS_DIR,
        event_bus: EventBus | None = None,
    ):
        self.extensions_dir = extensions_dir
        self.event_bus = event_bus or EventBus()
        self.extensions: dict[str, Extension] = {}
        self._apis: dict[str, ExtensionAPI] = {}

    def discover(self) -> list[Extension]:
        """发现所有扩展。"""
        if not self.extensions_dir.exists():
            return []

        discovered = []

        # 扫描 .py 文件
        for path in self.extensions_dir.glob("*.py"):
            if path.name.startswith("_"):
                continue
            name = path.stem
            ext = Extension(name=name, path=path)
            discovered.append(ext)
            self.extensions[name] = ext

        # 扫描子目录
        for path in self.extensions_dir.iterdir():
            if path.is_dir() and not path.name.startswith("_"):
                init_file = path / "__init__.py"
                if init_file.exists():
                    name = path.name
                    ext = Extension(name=name, path=init_file)
                    discovered.append(ext)
                    self.extensions[name] = ext

        return discovered

    def load(self, extension: Extension) -> bool:
        """加载单个扩展。"""
        try:
            # 动态加载模块
            spec = importlib.util.spec_from_file_location(
                f"bear_extension_{extension.name}",
                extension.path,
            )
            if spec is None or spec.loader is None:
                extension.error = "Failed to create module spec"
                return False

            module = importlib.util.module_from_spec(spec)
            sys.modules[f"bear_extension_{extension.name}"] = module
            spec.loader.exec_module(module)

            extension.module = module

            # 创建 API 并调用 setup
            api = ExtensionAPI(extension.name, self.event_bus)
            self._apis[extension.name] = api

            if extension.has_setup:
                module.setup(api)

            return True

        except Exception as e:
            extension.error = str(e)
            return False

    def load_all(self) -> dict[str, bool]:
        """加载所有扩展。"""
        results = {}
        for ext in self.extensions.values():
            results[ext.name] = self.load(ext)
        return results

    def reload(self, name: str) -> bool:
        """热重载扩展。"""
        if name not in self.extensions:
            return False

        ext = self.extensions[name]

        # 清除旧的
        if name in self._apis:
            self._apis[name].unregister_all()

        # 重新加载
        module_name = f"bear_extension_{name}"
        if module_name in sys.modules:
            del sys.modules[module_name]

        ext.module = None
        ext.error = ""

        # 重新创建 spec 和 module
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                ext.path,
            )
            if spec is None or spec.loader is None:
                ext.error = "Failed to create module spec"
                return False

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            ext.module = module

            # 创建新的 API 并调用 setup
            api = ExtensionAPI(name, self.event_bus)
            self._apis[name] = api

            if ext.has_setup:
                module.setup(api)

            return True

        except Exception as e:
            ext.error = str(e)
            return False

    def reload_all(self) -> dict[str, bool]:
        """热重载所有扩展。"""
        results = {}
        for name in self.extensions:
            results[name] = self.reload(name)
        return results

    def unload(self, name: str) -> bool:
        """卸载扩展。"""
        if name not in self.extensions:
            return False

        # 清除 API 注册
        if name in self._apis:
            self._apis[name].unregister_all()
            del self._apis[name]

        # 清除模块
        module_name = f"bear_extension_{name}"
        if module_name in sys.modules:
            del sys.modules[module_name]

        del self.extensions[name]
        return True

    def get_api(self, name: str) -> ExtensionAPI | None:
        """获取扩展的 API 实例。"""
        return self._apis.get(name)

    def list_extensions(self) -> list[dict[str, Any]]:
        """列出所有扩展信息。"""
        return [
            {
                "name": ext.name,
                "path": str(ext.path),
                "loaded": ext.module is not None,
                "enabled": ext.enabled,
                "error": ext.error,
                "loaded_at": ext.loaded_at,
            }
            for ext in self.extensions.values()
        ]


# ─── Global Loader ────────────────────────────────────────────────────────────


_global_loader: ExtensionLoader | None = None


def get_extension_loader(event_bus: EventBus | None = None) -> ExtensionLoader:
    """获取全局扩展加载器。"""
    global _global_loader
    if _global_loader is None:
        _global_loader = ExtensionLoader(event_bus=event_bus)
    return _global_loader
