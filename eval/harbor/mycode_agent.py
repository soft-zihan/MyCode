"""Harbor 侧的 MyCode 适配器：把 MyCode 装进题目容器，进程内跑一道题。

用 `-a eval.harbor.mycode_agent:MyCodeAgent` 加载，所以跑 harbor 的那个进程
PYTHONPATH 里必须有仓库根目录。

不走 MyCode 的 CLI：和 LOCA 一样进程内直接调 `run_agent_task`（见 agent_main.py），
这样两臂的差异只剩 agent 本身，不掺进 CLI 的参数解析和输出格式。

这个模块 import harbor，所以单测覆盖不到它——可判定的逻辑都放在 payload.py 里测，
这里只剩胶水，由真跑一遍验证。
"""

from __future__ import annotations

import json
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import override

from harbor.agents.installed.base import (
    BaseInstalledAgent,
    NonZeroAgentExitCodeError,
    with_prompt_template,
)
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from eval.harbor import payload

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULT_FILENAME = "mycode_result.json"
STDOUT_FILENAME = "mycode_stdout.txt"


class MyCodeAgent(BaseInstalledAgent):
    # 基类是 360s。apt 装 python3-venv + 建 venv + 装 60 多个 wheel，
    # 首次跑（apt 元数据是冷的）会超过 360s；trial.py:1573 读的是子类上的值。
    DEFAULT_SETUP_TIMEOUT_SEC = 900.0

    # 让 harbor 用 MYCODE_* 这两个名字找凭据，找不到再退回 provider 默认名
    # （model_name 写成 openrouter/... 时就是 OPENROUTER_API_KEY / OPENROUTER_BASE_URL）。
    MODEL_CONNECTION = ModelConnectionSpec(
        api_key_envs=(payload.API_KEY_ENV,),
        base_url_envs=(payload.BASE_URL_ENV,),
    )

    @staticmethod
    @override
    def name() -> str:
        return "mycode"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        if not await self._can_make_venv(environment):
            # cpp/java/go/rust/js 五类镜像用的是 buildpack-deps 自带的 python3.10，
            # 缺 python3-venv 时 `python3 -m venv` 会建出一个没有 pip 的空壳，
            # 报错还要等到下一步才出现。python 题的镜像已经装了 python3.11-venv，
            # 这时候走 apt 纯属白花一分钟。
            await self.ensure_system_dependencies(
                environment, ("python3", "python_venv", "python_pip")
            )
        await self.exec_as_root(
            environment, command=f"mkdir -p {shlex.quote(payload.CONTAINER_ROOT)}"
        )

        # 先在宿主暂存一份干净的树再整棵上传：直接把仓库根 upload_dir 进去会带上
        # __pycache__ 和 frontend/（406 MB）。
        staging = Path(tempfile.mkdtemp(prefix="mycode-payload-"))
        try:
            count = payload.stage_payload(REPO_ROOT, staging)
            await environment.upload_dir(staging, payload.CONTAINER_ROOT)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.logger.info(
            "uploaded %d MyCode files to %s", count, payload.CONTAINER_ROOT
        )

        # pip 走容器 env 里的 PIP_INDEX_URL（job 配置注入），不在代码里写死镜像源。
        for command in payload.install_commands():
            await self.exec_as_root(environment, command=command)

    async def _can_make_venv(self, environment: BaseEnvironment) -> bool:
        """镜像自带的 python3 能不能直接建出带 pip 的 venv。

        能就别碰 apt：python 题的 sources.list 里有 deadsnakes PPA，
        `ensure_system_dependencies` 会先跑 `apt-get update` 去打
        ppa.launchpadcontent.net，实测那次 503 直接让整个 trial 记异常
        —— agent 一行代码都没跑到。少一次 apt 就少一个这样的失败面，
        顺带省掉 python 题约 40s 的安装时间。
        """
        result = await environment.exec(
            command="python3 -c 'import venv, ensurepip'", timeout_sec=30
        )
        return result.return_code == 0

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        connection = self.model_connection
        if not connection.api_key or not connection.configured_base_url:
            # base_url 必须显式给：resolve_model_connection 在缺环境变量时会退回
            # provider 的默认域名（openrouter.ai），那样请求会打到公网上去。
            raise ValueError(
                "MyCode 臂缺凭据：需要 API key 和 base_url 两项都能解析出来"
                f"（当前 api_key={'有' if connection.api_key else '无'}, "
                f"base_url={connection.configured_base_url!r}）。"
                f"设置 {payload.API_KEY_ENV} / {payload.BASE_URL_ENV}，"
                "或 OPENROUTER_API_KEY / OPENROUTER_BASE_URL。"
            )
        env = payload.container_env(
            api_key=connection.api_key,
            base_url=connection.configured_base_url,
        )

        # 工作目录问容器要，不写死 /app：题目镜像的 WORKDIR 是数据集自己的事。
        workdir = (
            await self.exec_as_agent(environment, command="pwd", env=env)
        ).stdout.strip()

        # 题目正文走文件（最长 12896 字符），不进命令行——命令行会原样印进 trial.log。
        await self._upload_config_text(
            environment,
            content=instruction,
            remote_path=payload.INSTRUCTION_PATH,
            filename="instruction.md",
        )

        exec_result = await self.exec_as_agent(
            environment,
            command=payload.run_command(
                model=payload.model_id(self.model_name), workdir=workdir
            ),
            env=env,
        )

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / STDOUT_FILENAME).write_text(
            f"{exec_result.stdout or ''}\n--- stderr ---\n{exec_result.stderr or ''}\n",
            encoding="utf-8",
        )

        local_result = self.logs_dir / RESULT_FILENAME
        await environment.download_file(payload.RESULT_PATH, local_result)
        data = json.loads(local_result.read_text(encoding="utf-8"))

        for field, value in payload.context_updates(data).items():
            setattr(context, field, value)

        if failure := payload.hard_failure(data):
            raise NonZeroAgentExitCodeError(f"MyCode 没有任何产出：{failure}")
        if data.get("error"):
            self.logger.warning(
                "MyCode 报了错但已有产出，这次结果照常交给 verifier 判分：%s",
                data["error"],
            )
