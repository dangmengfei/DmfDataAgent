"""StarRocks 复用 MySQL 连接（同协议）的派生逻辑测试。

核心结论：StarRocks FE 走 MySQL 线协议，因此未显式配置 StarRocks 时，
``Settings.effective_starrocks`` 自动复用 MySQL 业务库连接并把方言切到 mysql，
无需独立连接串。本测试只校验「派生参数是否正确」，不发起真实网络连取。
"""

from dmf_agent.config.settings import (
    MySQLSettings,
    Settings,
    StarRocksSettings,
)


def _settings(db_enabled: bool, db_host: str, sr_host: str = "") -> Settings:
    return Settings(
        db=MySQLSettings(
            enabled=db_enabled,
            host=db_host,
            port=3306,
            user="dmf_agent",
            password="dmf_agent",
            database="dmf_agent",
        ),
        starrocks=StarRocksSettings(host=sr_host, dialect="starrocks"),
    )


def test_fallback_reuses_mysql_when_starrocks_unconfigured():
    s = _settings(db_enabled=True, db_host="127.0.0.1", sr_host="")
    eff = s.effective_starrocks
    # 复用 MySQL 连接参数
    assert eff.host == "127.0.0.1"
    assert eff.port == 3306
    assert eff.user == "dmf_agent"
    assert eff.password == "dmf_agent"
    assert eff.database == "dmf_agent"
    # 方言自动切 mysql，安全/限流参数沿用原始 starrocks 配置
    assert eff.dialect == "mysql"
    assert eff.max_rows == StarRocksSettings().max_rows
    assert eff.query_timeout == StarRocksSettings().query_timeout
    # 派生后视为已配置
    assert eff.configured is True


def test_explicit_starrocks_takes_precedence():
    s = _settings(db_enabled=True, db_host="127.0.0.1", sr_host="10.0.0.9")
    eff = s.effective_starrocks
    assert eff.host == "10.0.0.9"
    assert eff.dialect == "starrocks"  # 显式配置不触发 mysql 回退
    assert eff.configured is True


def test_no_fallback_when_db_disabled_memory_mode():
    s = _settings(db_enabled=False, db_host="", sr_host="")
    eff = s.effective_starrocks
    assert eff.host == ""  # 内存模式不回退，保留原始未配置对象
    assert eff.dialect == "starrocks"
    assert eff.configured is False


def test_no_fallback_when_db_host_empty():
    s = _settings(db_enabled=True, db_host="", sr_host="")
    eff = s.effective_starrocks
    assert eff.configured is False  # 无可达 MySQL 时不臆造连接
