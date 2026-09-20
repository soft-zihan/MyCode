"""Workspace file management APIs."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["workspace"])


class CreateFileRequest(BaseModel):
    path: str
    cwd: Optional[str] = None


class RenameRequest(BaseModel):
    old_path: str
    new_name: str
    cwd: Optional[str] = None


class MoveRequest(BaseModel):
    source_path: str
    target_path: str
    cwd: Optional[str] = None


class WriteFileRequest(BaseModel):
    path: str
    content: str
    cwd: Optional[str] = None


@router.get("/api/directories")
def api_list_directories(path: Optional[str] = None) -> dict[str, Any]:
    if path:
        target = Path(path).expanduser()
    else:
        target = Path.home()
    
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    
    dirs = []
    try:
        for item in sorted(target.iterdir()):
            if item.is_dir() and not item.name.startswith('.'):
                dirs.append({
                    "name": item.name,
                    "path": str(item),
                })
    except PermissionError:
        pass
    
    return {
        "current": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "directories": dirs,
    }


@router.get("/api/workspace/tree")
def api_workspace_tree(cwd: Optional[str] = None) -> dict[str, Any]:
    workspace_path = Path(cwd) if cwd else Path.cwd()
    if not workspace_path.exists():
        workspace_path = Path.cwd()
    
    # Directories to always exclude
    EXCLUDED_DIRS = {"node_modules", "__pycache__", ".venv", ".venv-1", "Library", "Applications", ".Trash"}
    # Hidden directories to include (not exclude)
    INCLUDED_HIDDEN = {".mycode"}
    
    def build_tree(path: Path, depth: int = 0) -> Optional[dict[str, Any]]:
        if depth > 10:
            return None
        
        result = {
            "name": path.name,
            "path": str(path.relative_to(workspace_path)),
            "type": "directory" if path.is_dir() else "file",
        }
        
        if path.is_dir():
            # Exclude hidden dirs unless they're in INCLUDED_HIDDEN
            if path.name.startswith(".") and path.name not in INCLUDED_HIDDEN:
                return None
            if path.name in EXCLUDED_DIRS:
                return None
            children = []
            try:
                all_children = list(path.iterdir())
                dirs = sorted([c for c in all_children if c.is_dir() and not (c.name in EXCLUDED_DIRS or (c.name.startswith(".") and c.name not in INCLUDED_HIDDEN))], key=lambda p: p.name.lower())
                files = sorted([c for c in all_children if c.is_file()], key=lambda p: p.name.lower())
                for child in dirs + files:
                    child_tree = build_tree(child, depth + 1)
                    if child_tree:
                        children.append(child_tree)
            except (PermissionError, InterruptedError, OSError):
                pass
            result["children"] = children
        else:
            try:
                result["size"] = path.stat().st_size
            except Exception:
                result["size"] = 0
        
        return result
    
    tree = build_tree(workspace_path)
    return tree or {"name": workspace_path.name, "path": ".", "type": "directory", "children": []}


@router.delete("/api/workspace/file")
def api_workspace_delete_file(path: str, cwd: Optional[str] = None) -> dict[str, Any]:
    workspace_path = Path(cwd) if cwd else Path.cwd()
    file_path = workspace_path / path
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    try:
        file_path.resolve().relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        if file_path.is_dir():
            shutil.rmtree(file_path)
        else:
            file_path.unlink()
        return {"success": True, "message": f"Deleted {path}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")


@router.post("/api/workspace/create")
def api_workspace_create_file(data: CreateFileRequest) -> dict[str, Any]:
    workspace_path = Path(data.cwd) if data.cwd else Path.cwd()
    file_path = workspace_path / data.path

    if not data.path or not data.path.strip():
        raise HTTPException(status_code=400, detail="File name cannot be empty")

    try:
        file_path.resolve().relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if file_path.exists():
        raise HTTPException(status_code=400, detail="File already exists")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()
        return {"success": True, "path": str(file_path.relative_to(workspace_path))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create file: {str(e)}")


@router.put("/api/workspace/file")
def api_workspace_write_file(data: WriteFileRequest) -> dict[str, Any]:
    workspace_path = Path(data.cwd) if data.cwd else Path.cwd()
    
    # 支持绝对路径
    if Path(data.path).is_absolute():
        file_path = Path(data.path)
    else:
        file_path = workspace_path / data.path

    if not data.path or not data.path.strip():
        raise HTTPException(status_code=400, detail="File path cannot be empty")

    # 只对相对路径进行工作区检查
    if not Path(data.path).is_absolute():
        try:
            file_path.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(data.content, encoding="utf-8")
        return {"success": True, "path": data.path, "size": len(data.content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")


@router.post("/api/workspace/rename")
def api_workspace_rename(data: RenameRequest) -> dict[str, Any]:
    workspace_path = Path(data.cwd) if data.cwd else Path.cwd()
    old_path = workspace_path / data.old_path
    
    if not old_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    try:
        old_path.resolve().relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    new_path = old_path.parent / data.new_name
    
    if new_path.exists():
        raise HTTPException(status_code=400, detail="Target already exists")
    
    try:
        old_path.rename(new_path)
        return {"success": True, "old_path": data.old_path, "new_path": str(new_path.relative_to(workspace_path))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename: {str(e)}")


@router.post("/api/workspace/move")
def api_workspace_move(data: MoveRequest) -> dict[str, Any]:
    workspace_path = Path(data.cwd) if data.cwd else Path.cwd()
    source = workspace_path / data.source_path
    target = workspace_path / data.target_path

    if not source.exists():
        raise HTTPException(status_code=404, detail="Source not found")

    try:
        source.resolve().relative_to(workspace_path.resolve())
        target.resolve().relative_to(workspace_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if target.exists():
        raise HTTPException(status_code=400, detail="Target already exists")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return {"success": True, "source": data.source_path, "target": data.target_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to move: {str(e)}")


@router.get("/api/workspace/file")
def api_workspace_file(path: str, cwd: Optional[str] = None) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = (Path(cwd) if cwd else Path.cwd()) / path
    
    # 如果文件不存在，检查是否是 hidden agent 的 override 文件
    if not file_path.exists():
        # 检查是否在 ~/.mycode/agents/ 目录下
        user_agents_dir = Path.home() / ".mycode" / "agents"
        if str(file_path.parent) == str(user_agents_dir) and file_path.suffix == ".md":
            # 从 prompt_registry 获取初始内容
            try:
                from agents.core.prompt_registry import get_prompt
                agent_name = file_path.stem
                prompt = get_prompt(f"hidden:{agent_name}")
                if prompt:
                    return {
                        "path": path,
                        "content": prompt.content,
                        "size": len(prompt.content),
                    }
            except Exception:
                pass
        raise HTTPException(status_code=404, detail="File not found")
    
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    
    try:
        content = file_path.read_text(encoding="utf-8")
        result = {
            "path": path,
            "content": content,
            "size": file_path.stat().st_size,
        }
        
        if ".mycode/wiki/" in str(file_path) and file_path.suffix == ".md":
            try:
                from agents.memory.frontmatter import parse_frontmatter
                meta, body = parse_frontmatter(content)
                result["frontmatter"] = meta
                result["content"] = body
            except Exception:
                pass
        
        return result
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Binary file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
