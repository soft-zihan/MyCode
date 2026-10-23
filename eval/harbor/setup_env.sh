#!/usr/bin/env bash
# 一次性把 WSL 里跑 Harbor coding-agent benchmark 需要的环境准备好。
#
# 幂等：每一步都先检查再动手，重复跑不会破坏已有配置。
# 只影响 WSL 里的原生 dockerd（`docker context ls` 里的 default），
# 不碰 Docker Desktop 那个独立 daemon 和它里面的容器。
#
# 用法： bash eval/harbor/setup_env.sh
set -uo pipefail

DATASET_COMMIT=f30b14415dd733c83627204bad0af69a89ceb46f
DATASET_REPO=laude-institute/harbor-datasets
DATASET_DIR="${HARBOR_DATASET_DIR:-$HOME/.mycode/harbor-datasets}"
JOBS_DIR="${HARBOR_JOBS_DIR:-$HOME/.mycode/harbor_jobs}"
BASE_IMAGE=buildpack-deps:jammy
REGISTRY_MIRROR=https://docker.1ms.run
APT_MIRROR=https://mirrors.aliyun.com/ubuntu
HOST_PROXY_PORT="${HARBOR_HOST_PROXY_PORT:-7897}"
COMPOSE_VERSION="${HARBOR_COMPOSE_VERSION:-v5.6.0}"
BUILDX_VERSION="${HARBOR_BUILDX_VERSION:-v0.38.0}"
CLI_PLUGINS_DIR=/usr/local/lib/docker/cli-plugins
DL_DIR="$HOME/.mycode/dl"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '   \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 0. 前置检查
step "0. 前置检查"
[ -f /proc/version ] && grep -qi microsoft /proc/version || die "这个脚本要在 WSL 里跑"
ok "在 WSL 内"

command -v docker >/dev/null || die "没装 docker engine（WSL 内原生安装，不要用 Docker Desktop）"
if ! docker info >/dev/null 2>&1; then
  if sg docker -c "docker info" >/dev/null 2>&1; then
    warn "当前 shell 不在 docker 组，后续命令用 sg docker -c 包一层（重开 shell 可解决）"
    DOCKER="sg docker -c"
  else
    die "dockerd 没起来"
  fi
else
  DOCKER="docker"
fi
ok "dockerd 可用：$($DOCKER 'docker info --format "{{.Name}} root={{.DockerRootDir}}"' 2>/dev/null)"

$DOCKER "docker ps -aq" | grep -q . && \
  warn "这个 daemon 里有容器，重启 docker 会影响它们（确认不是你要保护的东西再往下走）"

# ------------------------------------------------- 0b. docker compose / buildx 插件
step "0b. docker compose / buildx 插件"

# harbor 靠 `docker compose build|up` 起题目容器，镜像预热靠
# `docker buildx build --output type=cacheonly`（见 runner.py 的 prewarm_command）。
# 原生 docker engine **不自带**这两个插件。本机曾经是从 Docker Desktop 的 WSL 挂载
# (/mnt/wsl/docker-desktop/cli-tools/...) 借软链用的，Docker Desktop 一关，16 个软链全成死链，
# 而报错极难认：`docker compose ... build` → `unknown flag: --project-name`，返回码 125，
# harbor 只报一个 RuntimeError，看不出是插件没了。所以这里显式检查并装官方二进制。
[ "$(uname -m)" = "x86_64" ] || die "下面的插件下载地址写死了 amd64，本机是 $(uname -m)，请先改 URL"

mkdir -p "$DL_DIR"
PROXY_ARG=()
if curl -sS -m 5 -o /dev/null "http://127.0.0.1:$HOST_PROXY_PORT" 2>/dev/null; then
  PROXY_ARG=(-x "http://127.0.0.1:$HOST_PROXY_PORT")
  ok "GitHub 下载走宿主代理 127.0.0.1:$HOST_PROXY_PORT"
fi

fetch_plugin() {  # $1=插件文件名（docker-compose / docker-buildx） $2=下载 URL
  local name=$1 url=$2
  sudo -n mkdir -p "$CLI_PLUGINS_DIR" || die "需要免密 sudo 才能写 $CLI_PLUGINS_DIR"
  # 死链也得先删：install 不会覆盖悬空软链的目标
  if [ -L "$CLI_PLUGINS_DIR/$name" ] || [ -e "$CLI_PLUGINS_DIR/$name" ]; then
    sudo -n rm -f "$CLI_PLUGINS_DIR/$name"
  fi
  # 下载落 ~/.mycode/dl 而不是 /tmp：WSL 的 /tmp 会被清，实测过下到一半文件就没了
  curl -sS -fL --retry 3 --max-time 1500 "${PROXY_ARG[@]}" -o "$DL_DIR/$name" "$url" \
    || die "下载失败：$url"
  file "$DL_DIR/$name" | grep -q ELF \
    || die "下下来的不是 ELF 二进制（多半是错误页或不完整）：$DL_DIR/$name"
  sudo -n install -m 0755 "$DL_DIR/$name" "$CLI_PLUGINS_DIR/$name" || die "安装失败：$name"
}

if $DOCKER "docker compose version" >/dev/null 2>&1; then
  ok "compose 可用：$($DOCKER 'docker compose version' 2>/dev/null | head -1)"
else
  warn "compose 不可用，装官方二进制 $COMPOSE_VERSION"
  fetch_plugin docker-compose \
    "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/docker-compose-linux-x86_64"
  $DOCKER "docker compose version" >/dev/null 2>&1 || die "装完 compose 还是不可用"
  ok "compose 已装：$($DOCKER 'docker compose version' 2>/dev/null | head -1)"
fi

if $DOCKER "docker buildx version" >/dev/null 2>&1; then
  ok "buildx 可用：$($DOCKER 'docker buildx version' 2>/dev/null | head -1)"
else
  warn "buildx 不可用，装官方二进制 $BUILDX_VERSION"
  fetch_plugin docker-buildx \
    "https://github.com/docker/buildx/releases/download/$BUILDX_VERSION/buildx-$BUILDX_VERSION.linux-amd64"
  $DOCKER "docker buildx version" >/dev/null 2>&1 || die "装完 buildx 还是不可用"
  ok "buildx 已装：$($DOCKER 'docker buildx version' 2>/dev/null | head -1)"
fi

command -v harbor >/dev/null || export PATH="$HOME/.local/bin:$PATH"
command -v harbor >/dev/null || die "没装 harbor。装法见 eval/harbor/README.md 第 1.3 节"
ok "harbor $(harbor --version 2>/dev/null | head -1)"

HOSTIP=$(hostname -I | tr ' ' '\n' | grep -E '^(10\.|192\.168\.)' | head -1)
[ -n "$HOSTIP" ] || die "探测不到宿主 LAN IP，容器就用不上宿主代理"
if curl -sS -m 5 -o /dev/null "http://$HOSTIP:$HOST_PROXY_PORT" 2>/dev/null; then :; fi
ok "宿主 LAN IP = $HOSTIP（容器通过 http://$HOSTIP:$HOST_PROXY_PORT 用宿主代理）"
warn "这个 IP 是 DHCP 的，变了要重跑本脚本或让 runner 重新探测"

# ------------------------------------------------- 1. dockerd 代理 + 镜像站
step "1. dockerd 出网配置"

PROXY_DROPIN=/etc/systemd/system/docker.service.d/http-proxy.conf
NEED_RESTART=0

if [ -f "$PROXY_DROPIN" ] && grep -q "$HOST_PROXY_PORT" "$PROXY_DROPIN"; then
  ok "代理 drop-in 已存在"
else
  sudo -n mkdir -p /etc/systemd/system/docker.service.d || die "需要免密 sudo"
  [ -f "$PROXY_DROPIN" ] && sudo -n cp "$PROXY_DROPIN" "$PROXY_DROPIN.bak.$(date +%s)"
  sudo -n tee "$PROXY_DROPIN" >/dev <<EOF
[Service]
Environment="HTTP_PROXY=http://127.0.0.1:$HOST_PROXY_PORT"
Environment="HTTPS_PROXY=http://127.0.0.1:$HOST_PROXY_PORT"
Environment="NO_PROXY=localhost,127.0.0.1,10.*,172.16.*,172.17.*,192.168.*,docker.1ms.run,docker.xuanyuan.me,mirrors.aliyun.com,mirrors.tuna.tsinghua.edu.cn,archive.ubuntu.com,security.ubuntu.com,cherygpt.com,.cherygpt.com,.aliyuncs.com"
EOF
  ok "写入代理 drop-in（原文件已备份）"
  NEED_RESTART=1
fi

DAEMON_JSON=/etc/docker/daemon.json
if [ -f "$DAEMON_JSON" ] && grep -q 'registry-mirrors' "$DAEMON_JSON"; then
  ok "registry-mirrors 已配置：$(grep -o 'https://[^"]*' "$DAEMON_JSON" | tr '\n' ' ')"
else
  if [ -f "$DAEMON_JSON" ]; then
    warn "$DAEMON_JSON 已存在且没有 registry-mirrors，不覆盖。请手工加上："
    warn "  {\"registry-mirrors\": [\"$REGISTRY_MIRROR\"]}"
  else
    sudo -n tee "$DAEMON_JSON" >/dev <<EOF
{
  "registry-mirrors": [
    "$REGISTRY_MIRROR",
    "https://docker.xuanyuan.me"
  ]
}
EOF
    ok "写入 registry-mirrors（走代理拉 Docker Hub 只有 ~250 KB/s，镜像站能到 MB/s）"
    NEED_RESTART=1
  fi
fi

if [ "$NEED_RESTART" = 1 ]; then
  sudo -n systemctl daemon-reload
  sudo -n systemctl restart docker
  sleep 5
  ok "dockerd 已重启"
fi

# --------------------------------------------------- 2. 基础镜像 + 换 apt 源
step "2. 基础镜像 $BASE_IMAGE（换成国内 HTTPS apt 源）"

# 题目 Dockerfile 全部 FROM buildpack-deps:jammy，构建时 apt-get update 打的是
# archive.ubuntu.com。直连那个地址的 HTTP 响应会被中途改写（apt 报 NOSPLIT / BADSIG），
# 走代理又要 4 分钟。所以把本地这个 tag 换成「已配好 aliyun HTTPS 源」的同名镜像，
# 225 个题目的 Dockerfile 一个字都不用改。
if $DOCKER "docker image inspect $BASE_IMAGE" >/dev/null 2>&1 && \
   $DOCKER "docker run --rm $BASE_IMAGE grep -q mirrors.aliyun.com /etc/apt/sources.list" >/dev/null 2>&1; then
  ok "$BASE_IMAGE 已经是换过源的版本"
else
  $DOCKER "docker image inspect $BASE_IMAGE" >/dev/null 2>&1 || {
    echo "   拉取 $REGISTRY_MIRROR/library/$BASE_IMAGE ..."
    $DOCKER "docker pull $REGISTRY_MIRROR/library/$BASE_IMAGE" | tail -3 ||
      die "拉不动。换个镜像站，见 README 第 1.2b 节的存活表"
    $DOCKER "docker tag $REGISTRY_MIRROR/library/$BASE_IMAGE $BASE_IMAGE"
    ok "已拉取并打成 $BASE_IMAGE"
  }
  REBASE_DIR=$(mktemp -d)
  cat > "$REBASE_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE

RUN sed -i \\
      -e 's|http://archive.ubuntu.com/ubuntu|$APT_MIRROR|g' \\
      -e 's|http://security.ubuntu.com/ubuntu|$APT_MIRROR|g' \\
      /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get -y --no-install-recommends install ca-certificates \\
 && rm -rf /var/lib/apt/lists/*
EOF
  echo "   构建换源后的 $BASE_IMAGE ..."
  S=$(date +%s)
  $DOCKER "docker build --pull=false -t $BASE_IMAGE $REBASE_DIR" | tail -4 ||
    die "换源构建失败"
  ok "换源完成，用时 $(( $(date +%s) - S ))s"
  rm -rf "$REBASE_DIR"
fi

echo "   验证 apt 速度："
$DOCKER "docker run --rm $BASE_IMAGE bash -c 'apt-get update -qq 2>&1 | tail -2; apt-cache stats | head -1'" 2>&1 | sed 's/^/     /'

# --------------------------------------------------------- 3. 数据集落地
step "3. 数据集 aider-polyglot（225 题）"

AP="$DATASET_DIR/datasets/aider_polyglot"
if [ -d "$AP" ] && [ "$(ls "$AP" | wc -l)" -ge 225 ]; then
  ok "已在 $AP（$(ls "$AP" | wc -l) 个 task 目录）"
else
  # 不用 git clone：harbor 走的是 git clone --filter=blob:none，
  # 本机 git 通过代理稳定报 GnuTLS recv error (-9)。curl 拉 tarball 反而稳。
  mkdir -p "$DATASET_DIR"
  TARBALL=$(mktemp --suffix=.tar.gz)
  URL="https://codeload.github.com/$DATASET_REPO/tar.gz/$DATASET_COMMIT"
  echo "   下载 $URL"
  S=$(date +%s)
  curl -sS --retry 3 --retry-delay 3 -x "http://127.0.0.1:$HOST_PROXY_PORT" \
    -o "$TARBALL" "$URL" || die "tarball 下载失败"
  ok "下载完成 $(du -h "$TARBALL" | cut -f1)，用时 $(( $(date +%s) - S ))s"
  tar -xzf "$TARBALL" -C "$DATASET_DIR" --strip-components=1 || die "解压失败"
  rm -f "$TARBALL"
  ok "解压到 $DATASET_DIR（commit ${DATASET_COMMIT:0:12}）"
fi

mkdir -p "$JOBS_DIR"
ok "jobs 目录：$JOBS_DIR"

# ------------------------------------------------------------- 4. 端到端自检
step "4. 自检"
echo "   harbor 能否解析本地数据集："
$DOCKER "harbor run -p $AP -i polyglot_python_two-bucket -a nop -m openai/x --dry-run -o $JOBS_DIR -y" 2>&1 \
  | tail -3 | sed 's/^/     /'

step "完成"
cat <<EOF
   数据集： $AP
   jobs：   $JOBS_DIR
   基础镜像： $BASE_IMAGE（apt 源 = $APT_MIRROR）

   下一步跑评测（在仓库根目录，WSL 里没有 python 这个命令）：
     .venv/bin/python -m eval.harbor.runner --arm both --stage probe --dry-run
     .venv/bin/python -m eval.harbor.runner --arm both --stage probe
   两臂对比： .venv/bin/python -m eval.harbor.compare --stage probe
   实测事实与踩坑记录： eval/harbor/README.md
EOF
