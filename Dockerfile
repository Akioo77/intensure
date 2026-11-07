# intensure 采集层 —— 容器镜像
#
# 设计取舍（重要）：
#   **容器里不装 Mininet、不造拓扑。**
#   采集层的定位是「观测别人的网络」—— 拓扑要么在宿主的 Colima VM 里，
#   要么在对方的机器上（联调），要么是真实园区设备。
#   把 Mininet 塞进容器要 --privileged + netns，代价大且和这个定位冲突。
#
#   拓扑怎么接进容器？
#     · Mininet 在宿主 VM：起 Mininet 时把控制器指向容器
#         mn --controller remote,ip=<intensure 容器 IP>,port=6653 ...
#     · Mininet 在对方机器：对方起 Mininet 时同样指向我们的可达地址
#     · 真实园区设备：交换机自己开放 OpenFlow 端口，控制器去连
#
# 依赖：requirements.txt（Flask / os-ken / eventlet / psycopg2-binary / requests）
# 基础镜像做成可覆盖的 —— 国内网络直接拉 Docker Hub 会失败：
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
# （从宿主和 Colima VM 里直连 Docker Hub 都不通，daocloud 镜像源可以）
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# 时区：项目里用 +08:00 的时间戳（datetime.now(timezone(timedelta(hours=8))))，
# 容器里保持同一时区，免得两边时间对不上
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl 给健康检查用；libpq5 是 psycopg2 的运行期依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖先装（改代码不用重装依赖）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 代码
COPY . /app

# 非 root 运行
RUN useradd --create-home --shell /bin/bash intensure \
    && chown -R intensure:intensure /app \
    && chmod +x docker/start-services.sh
USER intensure

# 状态文件与日志的落点（容器内 /tmp，与 state_server 的 --state-file 一致）
RUN mkdir -p /var/log/intensure /tmp

EXPOSE 8080

# 健康检查打的是采集层自己的 API
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS --noproxy '*' http://127.0.0.1:8080/api/health || exit 1

ENTRYPOINT ["/app/docker/start-services.sh"]
