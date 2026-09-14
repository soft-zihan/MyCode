from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class GitRepositoryError(Exception):
    pass


class GitRepositoryManager:
    def __init__(self, project_root: str | Path, snapshot_dir: str | Path):
        self.project_root = Path(project_root).resolve()
        self.snapshot_dir = Path(snapshot_dir)
        self.repo_hash = hashlib.sha256(str(self.project_root).encode()).hexdigest()[:16]
        self.repo_path = self.snapshot_dir / self.repo_hash
        self._lock = asyncio.Lock()

    async def ensure_repository(self) -> None:
        if self.repo_path.exists():
            return
        self.repo_path.mkdir(parents=True, exist_ok=True)
        await self._run_git(["init"])
        await self._configure_repository()
        await self._configure_alternates()

    async def _configure_repository(self) -> None:
        configs = [
            # 影子仓库模式：git dir 在 snapshot_dir，工作树指向用户项目
            ["core.worktree", str(self.project_root)],
            ["core.autocrlf", "false"],
            ["core.longpaths", "true"],
            ["core.symlinks", "true"],
            ["core.fsmonitor", "false"],
            ["feature.manyFiles", "true"],
            ["index.version", "4"],
            ["index.threads", "true"],
            ["core.untrackedCache", "true"],
            ["core.compression", "1"],
        ]
        for key, value in configs:
            await self._run_git(["config", key, value])

    async def _configure_alternates(self) -> None:
        user_git_dir = self.project_root / ".git"
        if not user_git_dir.exists():
            return
        objects_info = self.repo_path / ".git" / "objects" / "info"
        objects_info.mkdir(parents=True, exist_ok=True)
        alternates_file = objects_info / "alternates"
        alternates_file.write_text(str(user_git_dir / "objects") + "\n")
        user_index = user_git_dir / "index"
        if user_index.exists():
            our_index = self.repo_path / ".git" / "index"
            shutil.copy2(user_index, our_index)

    async def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        stdin: str | None = None,
        check: bool = True,
    ) -> str:
        work_dir = cwd or self.repo_path
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE if stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=stdin.encode() if stdin else None)
        if check and proc.returncode != 0:
            raise GitRepositoryError(
                f"Git command failed: git {' '.join(args)}\n"
                f"stdout: {stdout.decode()}\n"
                f"stderr: {stderr.decode()}"
            )
        return stdout.decode()

    async def run_git(self, args: list[str], stdin: str | None = None) -> str:
        return await self._run_git(args, stdin=stdin)

    async def run_git_with_worktree(
        self,
        args: list[str],
        stdin: str | None = None,
    ) -> str:
        return await self._run_git(args, cwd=self.project_root, stdin=stdin)

    async def refresh_index(self, scope: str = ".") -> list[str]:
        async with self._lock:
            tracked = await self._run_git(
                ["diff-files", "--name-only", "-z", "--", scope],
                check=False,
            )
            untracked = await self._run_git(
                ["ls-files", "--others", "--exclude-standard", "-z", "--", scope],
                check=False,
            )
            tracked_files = [f for f in tracked.split("\0") if f]
            untracked_files = [f for f in untracked.split("\0") if f]
            all_files = list(set(tracked_files + untracked_files))
            if not all_files:
                return []
            if untracked_files:
                await self._run_git(
                    [
                        "add",
                        "--all",
                        "--sparse",
                        "--pathspec-from-file=-",
                        "--pathspec-file-nul",
                    ],
                    stdin="\0".join(untracked_files) + "\0",
                )
            if tracked_files:
                await self._run_git(
                    [
                        "add",
                        "--all",
                        "--sparse",
                        "--pathspec-from-file=-",
                        "--pathspec-file-nul",
                    ],
                    stdin="\0".join(tracked_files) + "\0",
                )
            return all_files

    async def write_tree(self) -> str:
        async with self._lock:
            result = await self._run_git(["write-tree"])
            return result.strip()

    async def capture_tree(self, scope: str = ".") -> str:
        await self.refresh_index(scope)
        return await self.write_tree()

    async def restore_files(self, tree_id: str, files: list[str]) -> None:
        async with self._lock:
            for file in files:
                entry_exists = await self._check_tree_entry(tree_id, file)
                if entry_exists:
                    await self._run_git(["checkout", tree_id, "--", file])
                else:
                    file_path = self.project_root / file
                    if file_path.exists():
                        if file_path.is_dir():
                            shutil.rmtree(file_path)
                        else:
                            file_path.unlink()

    async def _check_tree_entry(self, tree_id: str, path: str) -> bool:
        result = await self._run_git(
            ["ls-tree", tree_id, "--", path],
            check=False,
        )
        return bool(result.strip())

    async def tree_diff(
        self,
        from_tree: str,
        to_tree: str,
    ) -> list[dict]:
        result = await self._run_git(
            ["diff", "--name-status", "-z", from_tree, to_tree],
            check=False,
        )
        if not result.strip():
            return []
        parts = result.split("\0")
        changes = []
        i = 0
        while i < len(parts) - 1:
            status = parts[i]
            if not status:
                i += 1
                continue
            status_code = status[0]
            path = parts[i + 1] if i + 1 < len(parts) else ""
            if status_code in ("A", "M", "D"):
                changes.append({
                    "status": {"A": "added", "M": "modified", "D": "deleted"}[status_code],
                    "path": path,
                })
            i += 2
        return changes

    async def tree_diff_patch(
        self,
        from_tree: str,
        to_tree: str,
        path: str,
        context: int = 3,
    ) -> str:
        result = await self._run_git(
            ["diff", f"--unified={context}", "--no-renames", from_tree, to_tree, "--", path],
            check=False,
        )
        return result

    async def list_tree_files(self, tree_id: str) -> list[str]:
        result = await self._run_git(
            ["ls-tree", "-r", "--name-only", tree_id],
            check=False,
        )
        return [f for f in result.splitlines() if f]

    async def get_tree_entry(self, tree_id: str, path: str) -> dict | None:
        result = await self._run_git(
            ["ls-tree", tree_id, "--", path],
            check=False,
        )
        if not result.strip():
            return None
        parts = result.strip().split("\t", 1)
        if len(parts) != 2:
            return None
        mode_blob = parts[0]
        file_path = parts[1]
        mode_type, blob = mode_blob.split()
        return {
            "path": file_path,
            "mode": mode_type,
            "blob": blob,
        }
