from __future__ import annotations

import pytest

from agents.tools.permissions import (
    check_permission,
    is_dangerous,
    READ_TOOLS,
    EDIT_TOOLS,
)


class TestCheckPermission:

    def test_bypass_allows_everything(self):
        for tool in ["run_shell", "write_file", "edit_file", "skill_evolve"]:
            r = check_permission(tool, {}, "bypassPermissions")
            assert r["action"] == "allow"

    @pytest.mark.parametrize("tool", sorted(READ_TOOLS))
    def test_read_tools_always_allowed_in_default(self, tool):
        r = check_permission(tool, {}, "default")
        assert r["action"] == "allow"

    def test_default_write_existing_file_allowed(self, tmp_path):
        f = tmp_path / "existing.py"
        f.write_text("x")
        r = check_permission("write_file", {"file_path": str(f)}, "default")
        assert r["action"] == "allow"

    def test_default_write_new_file_needs_confirm(self):
        r = check_permission("write_file", {"file_path": "/nonexistent/new.py"}, "default")
        assert r["action"] == "confirm"

    def test_default_edit_nonexistent_file_needs_confirm(self):
        r = check_permission("edit_file", {"file_path": "/nonexistent/missing.py"}, "default")
        assert r["action"] == "confirm"

    def test_default_dangerous_shell_needs_confirm(self):
        r = check_permission("run_shell", {"command": "rm -rf /"}, "default")
        assert r["action"] == "confirm"

    def test_default_safe_shell_allowed(self):
        r = check_permission("run_shell", {"command": "ls -la"}, "default")
        assert r["action"] == "allow"

    def test_accept_edits_write_allowed(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x")
        r = check_permission("write_file", {"file_path": str(f)}, "acceptEdits")
        assert r["action"] == "allow"

    def test_accept_edits_dangerous_shell_still_confirm(self):
        r = check_permission("run_shell", {"command": "rm -rf /"}, "acceptEdits")
        assert r["action"] == "confirm"

    def test_plan_mode_allows_readonly_shell(self):
        r = check_permission("run_shell", {"command": "ls"}, "plan")
        assert r["action"] == "allow"

    def test_plan_mode_blocks_mutating_shell(self):
        r = check_permission("run_shell", {"command": "rm -rf /tmp/x"}, "plan")
        assert r["action"] == "deny"

    def test_plan_mode_blocks_edit_tools(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x")
        r = check_permission("write_file", {"file_path": str(f)}, "plan")
        assert r["action"] == "deny"

    def test_plan_mode_allows_plan_file_edit(self, tmp_path):
        plan = tmp_path / "plan.md"
        r = check_permission(
            "write_file",
            {"file_path": str(plan)},
            "plan",
            plan_dir=str(tmp_path),
        )
        assert r["action"] == "allow"

    def test_plan_mode_allows_read_tools(self):
        r = check_permission("read_file", {}, "plan")
        assert r["action"] == "allow"

    def test_plan_mode_allows_enter_exit(self):
        for tool in ("enter_plan_mode", "exit_plan_mode"):
            r = check_permission(tool, {}, "plan")
            assert r["action"] == "allow"

    def test_dont_ask_denies_confirm_actions(self):
        r = check_permission("run_shell", {"command": "rm -rf /"}, "dontAsk")
        assert r["action"] == "deny"

    def test_dont_ask_allows_read_tools(self):
        r = check_permission("read_file", {}, "dontAsk")
        assert r["action"] == "allow"

    def test_skill_evolve_needs_confirm(self):
        r = check_permission("skill_evolve", {"skill_name": "test"}, "default")
        assert r["action"] == "confirm"

    def test_skill_create_needs_confirm(self):
        r = check_permission("skill_create", {"name": "test"}, "default")
        assert r["action"] == "confirm"


class TestDangerousCommands:

    @pytest.mark.parametrize("command", [
        "rm -rf /",
        "rm file.txt",
        "git push origin main",
        "git reset --hard HEAD",
        "git clean -fd",
        "git checkout .",
        "sudo apt install",
        "kill -9 1234",
        "pkill python",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        "reboot",
        "shutdown -h now",
        "> /dev/sda",
        "del file.txt",
        "rmdir /some/dir",
        "taskkill /F /IM python.exe",
        "Remove-Item -Recurse",
        "Stop-Process -Name python",
    ])
    def test_detected_as_dangerous(self, command):
        assert is_dangerous(command) is True

    @pytest.mark.parametrize("command", [
        "ls -la",
        "cat file.txt",
        "git status",
        "git log --oneline",
        "git diff",
        "npm install",
        "npm test",
        "python script.py",
        "echo hello",
        "grep -r pattern .",
        "find . -name '*.py'",
    ])
    def test_safe_commands_not_flagged(self, command):
        assert is_dangerous(command) is False


class TestCustomPermissionRules:
    """自定义权限规则 — settings.json 驱动（项目级 {cwd}/.mycode/settings.json）。"""

    def _write_project_settings(self, permissions: dict) -> None:
        import json
        from pathlib import Path
        settings_dir = Path.cwd() / ".mycode"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.json").write_text(
            json.dumps({"permissions": permissions}), encoding="utf-8"
        )

    def test_allow_rule_skips_confirm_for_dangerous_shell(self):
        import agents.tools.permissions as perm_mod
        perm_mod.reset_permission_cache()
        try:
            # 无规则：危险命令需要确认
            assert check_permission("run_shell", {"command": "rm -rf build"}, "default")["action"] == "confirm"
            # allow 规则：直接放行
            self._write_project_settings({"allow": ["run_shell(rm *)"]})
            perm_mod.reset_permission_cache()
            r = check_permission("run_shell", {"command": "rm -rf build"}, "default")
            assert r["action"] == "allow"
        finally:
            perm_mod.reset_permission_cache()

    def test_deny_rule_blocks_in_default_mode(self):
        import agents.tools.permissions as perm_mod
        perm_mod.reset_permission_cache()
        try:
            self._write_project_settings({"deny": ["run_shell(npm *)"]})
            perm_mod.reset_permission_cache()
            r = check_permission("run_shell", {"command": "npm install"}, "default")
            assert r["action"] == "deny"
        finally:
            perm_mod.reset_permission_cache()

    def test_deny_takes_precedence_over_allow(self):
        import agents.tools.permissions as perm_mod
        perm_mod.reset_permission_cache()
        try:
            self._write_project_settings({
                "allow": ["run_shell(rm *)"],
                "deny": ["run_shell(rm -rf *)"],
            })
            perm_mod.reset_permission_cache()
            assert check_permission("run_shell", {"command": "rm -rf /tmp/x"}, "default")["action"] == "deny"
            assert check_permission("run_shell", {"command": "rm file.txt"}, "default")["action"] == "allow"
        finally:
            perm_mod.reset_permission_cache()

    def test_bypass_mode_ignores_rules(self):
        import agents.tools.permissions as perm_mod
        perm_mod.reset_permission_cache()
        try:
            self._write_project_settings({"deny": ["run_shell(rm *)"]})
            perm_mod.reset_permission_cache()
            r = check_permission("run_shell", {"command": "rm file.txt"}, "bypassPermissions")
            assert r["action"] == "allow"
        finally:
            perm_mod.reset_permission_cache()
