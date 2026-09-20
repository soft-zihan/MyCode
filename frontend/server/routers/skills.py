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


@router.post("/api/skill-evolution/evaluate")
async def api_skill_evolution_evaluate() -> dict[str, Any]:
    """手动触发验证门禁在线评测（仅确定性规则，不含 LLM judge）。

    自动路径：skill 变体变更（online ingest / pattern 编译）后会带 side_query
    触发含 LLM judge 的完整评测；此端点用于无活跃会话时的手动补评。
    """
    try:
        from agents.skills.skill_evaluator import evaluate_online_skill_evolution_async

        report = await evaluate_online_skill_evolution_async(side_query=None)
        return {"status": "ok", "report": report}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"skill evolution evaluation failed: {e}")


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


def _load_eval_report() -> dict[str, Any] | None:
    report_path = project_root / ".mycode" / "skill-evolution" / "online_eval_report.json"
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text())
    except Exception:
        return None


def _find_skill_in_report(report: dict[str, Any], skill_name: str) -> dict[str, Any] | None:
    skills = report.get("skills") if isinstance(report.get("skills"), list) else []
    for s in skills:
        if s.get("skill") == skill_name:
            return s
    return None


@router.get("/api/skill-evolution/status/{skill_name}")
def api_skill_evolution_status(skill_name: str) -> dict[str, Any]:
    report = _load_eval_report()
    if report is None:
        raise HTTPException(status_code=404, detail="No evaluation report available. Run evaluation first.")
    skill_data = _find_skill_in_report(report, skill_name)
    if skill_data is None:
        raise HTTPException(status_code=404, detail=f"Skill not found in report: {skill_name}")
    return {
        "skill_name": skill_name,
        "status": skill_data.get("status", "unknown"),
        "reasons": skill_data.get("reasons", []),
        "rule_summary": skill_data.get("eval", {}),
        "replay_pool_size": skill_data.get("replay", {}).get("count", 0),
        "replay": skill_data.get("replay", {}),
        "champion": skill_data.get("artifacts", {}).get("promotion", {}),
        "rules": skill_data.get("eval", {}).get("rules", []),
        "current_version": skill_data.get("current_version", ""),
        "lineage_id": skill_data.get("lineage_id", ""),
    }


@router.get("/api/skill-evolution/replay-pool/{skill_name}")
def api_skill_evolution_replay_pool(skill_name: str) -> dict[str, Any]:
    from agents.skills.eval_replay import _build_replay_pool, _rows_by_skill, _active_skill_snapshots
    from agents.skills.eval_rules import _compile_eval_rules
    from agents.skills.eval_champion import _lineage_id_for_skill
    from agents._utils import read_jsonl as _read_jsonl, read_json as _read_json
    from agents.skills.skill_file_ops import ONLINE_PROVENANCE_LOG, get_evolution_dir

    root = get_evolution_dir()
    provenance_rows = _read_jsonl(root / ONLINE_PROVENANCE_LOG)
    provenance_index = _read_json(root / "online_provenance_index.json", {})
    grouped_rows = _rows_by_skill(provenance_rows)
    lineage_raw = provenance_index.get(skill_name, {}) if isinstance(provenance_index, dict) else {}
    lineage = lineage_raw if isinstance(lineage_raw, dict) else {}
    active_skills = _active_skill_snapshots()
    snapshot = active_skills.get(skill_name, {"name": skill_name, "description": "", "when_to_use": "", "instructions": ""})

    pool = _build_replay_pool(skill_name, grouped_rows.get(skill_name, []), lineage, freeze=False)
    return {
        "skill_name": skill_name,
        "total_samples": len(pool),
        "dev_count": len([s for s in pool if s.get("split") == "mutate_dev"]),
        "test_count": len([s for s in pool if s.get("split") == "promotion_test"]),
        "samples": [
            {
                "sample_id": s.get("sample_id", ""),
                "source_type": s.get("source_type", ""),
                "split": s.get("split", ""),
                "time": s.get("time", ""),
                "ok": s.get("ok", True),
                "latest_user": (s.get("latest_user", "") or "")[:200],
                "latest_assistant": (s.get("latest_assistant", "") or "")[:200],
            }
            for s in pool
        ],
    }


@router.get("/api/skill-evolution/champion/{skill_name}")
def api_skill_evolution_champion(skill_name: str) -> dict[str, Any]:
    from agents.skills.eval_champion import _lineage_id_for_skill, _load_champion
    lineage_id = _lineage_id_for_skill(skill_name)
    champion = _load_champion(lineage_id)
    if not champion:
        return {"skill_name": skill_name, "has_champion": False}
    return {
        "skill_name": skill_name,
        "has_champion": True,
        "champion": {
            "version": champion.get("version", ""),
            "average_score": champion.get("average_score", 0),
            "hard_failures": champion.get("hard_failures", 0),
            "promoted_at": champion.get("promoted_at", ""),
            "summary": champion.get("summary", {}),
        },
    }


@router.get("/api/skill-evolution/provenance/{skill_name}")
def api_skill_evolution_skill_provenance(skill_name: str) -> list[dict[str, Any]]:
    provenance_path = project_root / ".mycode" / "skill-evolution" / "online_provenance.jsonl"
    if not provenance_path.exists():
        return []
    try:
        lines = provenance_path.read_text().splitlines()
        all_entries = [json.loads(line) for line in lines if line.strip()]
        return [e for e in all_entries if e.get("skill") == skill_name or e.get("skill_name") == skill_name]
    except Exception:
        return []
