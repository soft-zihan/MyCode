"""Skills management APIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from agents.skills.skills import discover_skills, get_skill_by_name, skill_stats

router = APIRouter(tags=["skills"])

project_root = Path(__file__).parent.parent.parent.parent


@router.get("/api/skills")
def api_list_skills() -> list[dict[str, Any]]:
    skills = discover_skills()
    return [
        {
            "name": getattr(s, 'name', 'unknown'),
            "description": getattr(s, 'description', ''),
            "skill_dir": getattr(s, 'skill_dir', ''),
            "model": getattr(s, 'model', None),
            "source": getattr(s, 'source', 'project'),
        }
        for s in skills
    ]


@router.get("/api/skills/stats")
def api_skill_stats() -> dict[str, Any]:
    return skill_stats()


@router.get("/api/skills/{skill_name}")
def api_get_skill(skill_name: str) -> dict[str, Any]:
    skill = get_skill_by_name(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    skill_dir = getattr(skill, 'skill_dir', '')
    raw_content = ''
    if skill_dir:
        skill_file = Path(skill_dir) / 'SKILL.md'
        if skill_file.exists():
            raw_content = skill_file.read_text()
    
    return {
        "name": getattr(skill, 'name', 'unknown'),
        "description": getattr(skill, 'description', ''),
        "skill_dir": skill_dir,
        "model": getattr(skill, 'model', None),
        "source": getattr(skill, 'source', 'project'),
        "prompt_template": getattr(skill, 'prompt_template', ''),
        "raw_content": raw_content,
    }


@router.put("/api/skills/{skill_name}")
def api_update_skill(skill_name: str, data: dict[str, Any]) -> dict[str, Any]:
    skill = get_skill_by_name(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    skill_dir = getattr(skill, 'skill_dir', '')
    if not skill_dir:
        raise HTTPException(status_code=400, detail="Skill directory not found")
    
    skill_file = Path(skill_dir) / 'SKILL.md'
    if not skill_file.exists():
        raise HTTPException(status_code=404, detail="SKILL.md not found")
    
    new_content = data.get('content', '')
    skill_file.write_text(new_content)
    
    import agents.skills as skills_module
    skills_module._cached_skills = None
    
    return {"status": "ok", "message": f"Skill {skill_name} updated"}


@router.delete("/api/skills/{skill_name}")
def api_delete_skill(skill_name: str) -> dict[str, Any]:
    skill = get_skill_by_name(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    skill_dir = getattr(skill, 'skill_dir', '')
    if not skill_dir:
        raise HTTPException(status_code=400, detail="Skill directory not found")
    
    import shutil
    try:
        shutil.rmtree(skill_dir)
        import agents.skills as skills_module
        skills_module._cached_skills = None
        return {"status": "ok", "message": f"Skill {skill_name} deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete skill: {str(e)}")


@router.get("/api/skill-evolution/report")
def api_skill_evolution_report() -> dict[str, Any]:
    report_path = project_root / ".mycode" / "skill-evolution" / "online_eval_report.json"
    if not report_path.exists():
        return {"status": "no_report"}
    try:
        report = json.loads(report_path.read_text())
        return {"status": "ok", "report": report}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/api/skill-evolution/provenance")
def api_skill_evolution_provenance() -> list[dict[str, Any]]:
    provenance_path = project_root / ".mycode" / "skill-evolution" / "online_provenance.jsonl"
    if not provenance_path.exists():
        return []
    try:
        lines = provenance_path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except Exception:
        return []


@router.get("/api/skill-evolution/usage")
def api_skill_evolution_usage() -> dict[str, Any]:
    usage_path = project_root / ".mycode" / "skill-evolution" / "skill_usage_stats.json"
    if not usage_path.exists():
        return {}
    try:
        return json.loads(usage_path.read_text())
    except Exception:
        return {}
