"""logging_setup.py — 统一的日志配置（P1-2）。

**为什么需要**：之前代码库里 1 个文件用 logging、23 个用 print()，
演示时输出噪声巨大且无法按级别过滤。

**设计要点**：
1. 输出走 **stderr** —— 这样 `cmd 2>&1 | tee xxx.log` 的既有脚本照样能拿到全部输出
   （demo_scripts/*.sh 全是这个模式），不会因为改造日志而破坏演示脚本。
2. 可选 file handler —— 落 `/var/log/intensure/intensure.log`，排障时能查历史。
3. **幂等** —— 重复调用不会叠加 handler，不会出现重复日志。
4. **默认 INFO** —— 改造前 print 的默认可见性保持不变。

用法：
    import logging
    from logging_setup import configure_logging
    logger = logging.getLogger(__name__)

    def main():
        configure_logging()          # 或 configure_logging(level='DEBUG', log_file=...)
        logger.info("启动 ...")

CLI 工具（demo.py / verify_live.py / seed_policies.py 等）**不改造** ——
它们的 stdout 就是对外接口，换成 logging 反而破坏使用方式。
"""
import logging
import os
import sys

# 日志格式: 时间 + 级别 + 模块名 + 内容
DEFAULT_FORMAT = '%(asctime)s %(levelname)-7s [%(name)s] %(message)s'
# 纯内容格式(给需要 grep 的场景用)
PLAIN_FORMAT = '%(message)s'

DEFAULT_LOG_FILE = '/var/log/intensure/intensure.log'

# 环境变量 INTENSURE_LOG_FILE 指向的路径会启用 file handler。
# 故意不默认开启 —— 测试/CI 里不该往 /var/log 写东西。
ENV_LOG_FILE = 'INTENSURE_LOG_FILE'

_configured = False


def configure_logging(level='INFO', log_file=None, plain=False,
                      logger_name='intensure'):
    """配置 root logger。

    参数:
        level:     日志级别，默认 INFO
        log_file:  日志文件路径；None 表示不落文件（除非设了环境变量
                   INTENSURE_LOG_FILE）。传 'default' 用
                   /var/log/intensure/intensure.log
        plain:     True = 不带时间/级别前缀（便于 grep）
        logger_name: 给本模块的 logger 加个统一前缀
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 清掉已有的 handler，避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = PLAIN_FORMAT if plain else DEFAULT_FORMAT

    # stderr —— 保证 `2>&1 | tee` 的脚本仍能拿到全部输出
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream)

    # 文件 handler（可选）
    if log_file is None:
        # 环境变量指定时才落文件
        env_file = os.environ.get(ENV_LOG_FILE)
        if env_file:
            log_file = env_file
    if log_file == 'default':
        log_file = DEFAULT_LOG_FILE
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.FileHandler(log_file, encoding='utf-8')
            fh.setFormatter(logging.Formatter(fmt))
            root.addHandler(fh)
        except (OSError, PermissionError) as e:
            # 落不了文件不能影响主流程（演示时 /var/log 可能没权限）
            root.warning('无法写日志文件 %s: %s', log_file, e)

    _configured = True


def get_logger(name=None, prefix='intensure'):
    """取一个带统一前缀的 logger。

    get_logger(__name__) -> 'intensure.controller_app'
    """
    if name and name.startswith(prefix):
        return logging.getLogger(name)
    if name:
        return logging.getLogger('%s.%s' % (prefix, name))
    return logging.getLogger(prefix)
