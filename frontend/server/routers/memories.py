"""Memory management APIs."""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.memory.memory import list_memories, save_memory, delete_memory, get_memory_dir

router = APIRouter(tags=["memories"])


class MemoryCreate(BaseModel):
    name: str
    description: str
    type: str
    content: str


class MemoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    hooks: Optional[list[str]] = None


@router.get("/api/memories")
def api_list_memories() -> list[dict[str, str]]:
    entries = list_memories()
    return [
        {
            "name": e.name,
            "description": e.description,
            "type": e.type,
            "filename": e.filename,
            "content": e.content,
        }
        for e in entries
    ]


@router.get("/api/memories/{filename}")
def api_get_memory(filename: str) -> dict[str, str]:
    entries = list_memories()
    for e in entries:
        if e.filename == filename:
            return {
                "name": e.name,
                "description": e.description,
                "type": e.type,
                "filename": e.filename,
                "content": e.content,
            }
    raise HTTPException(status_code=404, detail="Memory not found")


@router.post("/api/memories")
def api_create_memory(data: MemoryCreate) -> dict[str, str]:
    if data.type not in {"user", "feedback", "project", "reference"}:
        raise HTTPException(status_code=400, detail="Invalid memory type")
    filename = save_memory(data.name, data.description, data.type, data.content)
    return {"filename": filename}


@router.delete("/api/memories/{filename}")
def api_delete_memory(filename: str) -> dict[str, bool]:
    success = delete_memory(filename)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"success": True}


@router.put("/api/memories/{filename}")
def api_update_memory(filename: str, data: MemoryUpdate) -> dict[str, str]:
    memory_dir = get_memory_dir()
    memory_path = memory_dir / filename
    
    if not memory_path.exists():
        raise HTTPException(status_code=404, detail="Memory not found")
    
    existing = memory_path.read_text(encoding="utf-8")
    
    fm_match = re.match(r'^---\n(.*?)\n---\n(.*)$', existing, re.DOTALL)
    
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2)
        lines = fm_text.split('\n')
        fm_dict = {}
        for line in lines:
            if ':' in line:
                key, val = line.split(':', 1)
                fm_dict[key.strip()] = val.strip().strip('"').strip("'")
    else:
        fm_dict = {}
        body = existing
    
    if data.name is not None:
        fm_dict['name'] = data.name
    if data.description is not None:
        fm_dict['description'] = data.description
    if data.hooks is not None:
        fm_dict['hooks'] = ','.join(data.hooks)
    
    new_body = data.content if data.content is not None else body
    new_fm = '\n'.join(f'{k}: {v}' for k, v in fm_dict.items())
    new_content = f'---\n{new_fm}\n---\n{new_body}'
    
    if data.name is not None and data.name != fm_dict.get('name', ''):
        new_filename = f"{data.name.lower().replace(' ', '_')}.md"
        new_path = memory_dir / new_filename
        new_path.write_text(new_content, encoding="utf-8")
        memory_path.unlink()
        return {"filename": new_filename, "message": "Memory updated and renamed"}
    else:
        memory_path.write_text(new_content, encoding="utf-8")
        return {"filename": filename, "message": "Memory updated"}
