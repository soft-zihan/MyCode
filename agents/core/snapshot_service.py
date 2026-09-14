from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agents.core.git_repository import GitRepositoryManager

if TYPE_CHECKING:
    pass


@dataclass
class SnapshotManifest:
    id: str
    session_id: str
    tree_hash: str
    files: list[str]
    created_at: int
    label: str | None = None
    message_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "tree_hash": self.tree_hash,
            "files": self.files,
            "created_at": self.created_at,
            "label": self.label,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SnapshotManifest:
        return cls(
            id=data["id"],
            session_id=data["session_id"],
            tree_hash=data["tree_hash"],
            files=data.get("files", []),
            created_at=data["created_at"],
            label=data.get("label"),
            message_id=data.get("message_id"),
        )


@dataclass
class SnapshotSummary:
    id: str
    tree_hash: str
    file_count: int
    created_at: int
    label: str | None = None
    message_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tree_hash": self.tree_hash,
            "file_count": self.file_count,
            "created_at": self.created_at,
            "label": self.label,
            "message_id": self.message_id,
        }


@dataclass
class FileDiff:
    path: str
    status: str
    patch: str = ""
    additions: int = 0
    deletions: int = 0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "patch": self.patch,
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass
class SnapshotInspection:
    id: str
    tree_hash: str
    created_at: int
    label: str | None = None
    files: list[FileDiff] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tree_hash": self.tree_hash,
            "created_at": self.created_at,
            "label": self.label,
            "files": [f.to_dict() for f in self.files],
        }


class SnapshotService:
    def __init__(self, project_root: str | Path, snapshot_dir: str | Path):
        self.project_root = Path(project_root).resolve()
        self.snapshot_dir = Path(snapshot_dir)
        self._repo: GitRepositoryManager | None = None

    def _get_repo(self) -> GitRepositoryManager:
        if self._repo is None:
            self._repo = GitRepositoryManager(self.project_root, self.snapshot_dir)
        return self._repo

    def _manifests_dir(self) -> Path:
        return self._get_repo().repo_path / "manifests"

    async def _ensure_manifests_dir(self) -> Path:
        manifests_dir = self._manifests_dir()
        manifests_dir.mkdir(parents=True, exist_ok=True)
        return manifests_dir

    async def _save_manifest(self, manifest: SnapshotManifest) -> None:
        manifests_dir = await self._ensure_manifests_dir()
        manifest_file = manifests_dir / f"{manifest.id}.json"
        manifest_file.write_text(json.dumps(manifest.to_dict(), indent=2))

    async def _load_manifest(self, snapshot_id: str) -> SnapshotManifest:
        manifests_dir = self._manifests_dir()
        manifest_file = manifests_dir / f"{snapshot_id}.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found")
        data = json.loads(manifest_file.read_text())
        return SnapshotManifest.from_dict(data)

    def _generate_snapshot_id(self) -> str:
        return f"snap_{int(time.time())}_{secrets.token_hex(4)}"

    async def capture(
        self,
        session_id: str,
        label: str | None = None,
        message_id: str | None = None,
    ) -> SnapshotSummary:
        repo = self._get_repo()
        await repo.ensure_repository()
        tree_hash = await repo.capture_tree()
        files = await repo.list_tree_files(tree_hash)
        snapshot_id = self._generate_snapshot_id()
        manifest = SnapshotManifest(
            id=snapshot_id,
            session_id=session_id,
            tree_hash=tree_hash,
            files=files,
            created_at=int(time.time()),
            label=label,
            message_id=message_id,
        )
        await self._save_manifest(manifest)
        return SnapshotSummary(
            id=snapshot_id,
            tree_hash=tree_hash,
            file_count=len(files),
            created_at=manifest.created_at,
            label=label,
            message_id=message_id,
        )

    async def list(self, session_id: str | None = None) -> list[SnapshotSummary]:
        manifests_dir = self._manifests_dir()
        if not manifests_dir.exists():
            return []
        snapshots = []
        for manifest_file in manifests_dir.glob("*.json"):
            try:
                data = json.loads(manifest_file.read_text())
                manifest = SnapshotManifest.from_dict(data)
                if session_id and manifest.session_id != session_id:
                    continue
                snapshots.append(
                    SnapshotSummary(
                        id=manifest.id,
                        tree_hash=manifest.tree_hash,
                        file_count=len(manifest.files),
                        created_at=manifest.created_at,
                        label=manifest.label,
                        message_id=manifest.message_id,
                    )
                )
            except Exception:
                continue
        snapshots.sort(key=lambda s: s.created_at, reverse=True)
        return snapshots

    async def inspect(self, snapshot_id: str) -> SnapshotInspection:
        repo = self._get_repo()
        manifest = await self._load_manifest(snapshot_id)
        # capture_tree = refresh_index + write_tree：必须刷新 index，
        # 否则 write_tree 返回陈旧树，看不到磁盘上的最新变更
        current_tree = await repo.capture_tree()
        changes = await repo.tree_diff(manifest.tree_hash, current_tree)
        files = []
        for change in changes:
            patch = await repo.tree_diff_patch(
                manifest.tree_hash,
                current_tree,
                change["path"],
            )
            files.append(
                FileDiff(
                    path=change["path"],
                    status=change["status"],
                    patch=patch,
                )
            )
        return SnapshotInspection(
            id=manifest.id,
            tree_hash=manifest.tree_hash,
            created_at=manifest.created_at,
            label=manifest.label,
            files=files,
        )

    async def restore(self, snapshot_id: str, files: list[str] | None = None) -> list[str]:
        repo = self._get_repo()
        manifest = await self._load_manifest(snapshot_id)
        if files is None:
            files = manifest.files
        await repo.restore_files(manifest.tree_hash, files)
        return files

    async def get_manifest(self, snapshot_id: str) -> SnapshotManifest:
        return await self._load_manifest(snapshot_id)

    async def diff(
        self,
        from_snapshot_id: str,
        to_snapshot_id: str,
    ) -> list[FileDiff]:
        repo = self._get_repo()
        from_manifest = await self._load_manifest(from_snapshot_id)
        to_manifest = await self._load_manifest(to_snapshot_id)
        changes = await repo.tree_diff(from_manifest.tree_hash, to_manifest.tree_hash)
        files = []
        for change in changes:
            patch = await repo.tree_diff_patch(
                from_manifest.tree_hash,
                to_manifest.tree_hash,
                change["path"],
            )
            files.append(
                FileDiff(
                    path=change["path"],
                    status=change["status"],
                    patch=patch,
                )
            )
        return files
