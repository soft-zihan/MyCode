# syntax=docker/dockerfile:1

# ── 阶段 1：构建前端 ────────────────────────────────────────────────
# Node 必须 22：jsdom 30 依赖的 undici 会调 webidl.util.markAsUncloneable，
# 那是 Node 20.19.0 才 backport 的 API（同一原因见 .github/workflows/ci.yml 的注释）。
FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build

# 先只拷依赖清单：之后改源码不会击穿这一层缓存。
# 层缓存顺序是构建时长的主要变量，别把 COPY 源码写在 npm ci 前面。
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY frontend/ ./
# package.json 里 build = "tsc && vite build"，产物落在 /build/dist
RUN npm run build


# ── 阶段 2：运行层 ──────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# PYTHONPYCACHEPREFIX：只读根文件系统下 .pyc 不能写在源码旁边，统一挪到 /tmp
# PYTHONUNBUFFERED：日志直接进 kubectl logs，不滞留在缓冲区（start.sh 里同一理由）
# MYCODE_HOST：容器里必须绑 0.0.0.0，否则 Service 转不进来（auth.py 默认 127.0.0.1）
# MYCODE_TRACING：默认关，避免没有 Langfuse 密钥时每次启动都打一段异常栈
# XDG_CACHE_HOME：只读根之下任何想写缓存的库都得有个可写落点，指到 /tmp（Pod 里是 emptyDir）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    MYCODE_HOST=0.0.0.0 \
    MYCODE_TRACING=0 \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/home/app

WORKDIR /app

COPY requirements.txt ./
# 两个细节都是为了不让构建期的缓存变成镜像里的死重量：
#
# 1. cache mount 的路径必须是 /tmp/.cache/pip 而不是 /root/.cache/pip。
#    上面 ENV 设了 XDG_CACHE_HOME=/tmp/.cache，pip 认这个变量，所以它的 http 缓存
#    实际落在 /tmp/.cache/pip。挂错路径的后果是双份的：38MB 缓存进镜像层，
#    且后面那句 chown -R 会把它整体 copy-up 再复制一遍（实测多出 80MB 的一层）。
# 2. --no-compile：pip 默认会字节码编译，产物被 PYTHONPYCACHEPREFIX 引到
#    /tmp/pycache，实测 40MB。而 Pod 里 /tmp 挂的是 emptyDir，镜像内的
#    /tmp/pycache 会被整个遮住——这 40MB 在目标环境里永远读不到。
#    （PYTHONDONTWRITEBYTECODE=1 拦不住它：pip 是显式调 compileall 的。）
#
# 这里刻意**不设** PIP_NO_CACHE_DIR：缓存已经挂在镜像层之外，既不撑大镜像，
# 又能在 requirements.txt 变动时复用已下载的 wheel。本机 pip 那层实测 219s，
# 缓存能否命中是重建时长的主要变量。
RUN --mount=type=cache,target=/tmp/.cache/pip \
    pip install --no-compile -r requirements.txt

# 运行期只需要这些。刻意不含 eval/ tests/ mindmap/ docs/：
# main.py 对 eval.common.jobs / eval.common.tailer 的导入在 try/except 里且标注
# non-fatal，所以裁掉 eval/ 服务照常起，只是评测端点不可用——云端本就不该跑评测。
COPY agents/ ./agents/
COPY frontend/server/ ./frontend/server/
COPY frontend/openapi.json ./frontend/openapi.json
COPY VERSION ./VERSION
COPY mycode ./mycode
COPY --from=frontend-build /build/dist ./frontend/dist

# 非 root，uid/gid 固定 10001，与 deploy/base/deployment.yaml 的
# runAsUser / fsGroup 对齐。
# HOME 下预建的两个点目录只是兜底：K8s 里 HOME 整体挂 emptyDir、
# /home/app/.mycode 再嵌一层 PVC（理由见 deployment.yaml 的注释）。
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --home-dir /home/app --create-home \
      --shell /usr/sbin/nologin app \
 && mkdir -p /home/app/.mycode/sessions /home/app/.my-code /tmp/pycache /tmp/.cache \
 && chown -R 10001:10001 /home/app /tmp/pycache /tmp/.cache

USER 10001
EXPOSE 5555

# 用现有入口而不是 uvicorn 命令行：main.py 靠 sys.path.insert(project_root) 让
# `from routers import ...` 成立（routers 不是包，是 server 目录下的平铺模块）。
# 换成 `uvicorn frontend.server.main:app` 会 ImportError。
CMD ["python", "frontend/server/main.py"]
