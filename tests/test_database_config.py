"""Test database selection fails closed; no database connections are made."""

from pathlib import Path

import pytest

from scripts.test_database_config import read_test_database


def read_sit_database(path):
    return read_test_database(path, "SIT")


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "synthetic-dev-config.txt"
    path.write_text(content, encoding="utf-8")
    return path


def test_sit_parser_never_uses_uat_fields(tmp_path):
    path = _write(
        tmp_path,
        """开发环境 SIT 数据库
数据库地址：sit.example.test:5432
数据库名称：sit_test
用户名：sit_user
密码：synthetic:sit=password
UAT 数据库
数据库地址：uat.example.test:6432
数据库名称：uat_test
用户名：uat_user
密码：synthetic-uat-password
""",
    )
    assert read_sit_database(path) == {
        "host": "sit.example.test",
        "port": 5432,
        "name": "sit_test",
        "user": "sit_user",
        "password": "synthetic:sit=password",
    }


@pytest.mark.parametrize("heading", ["PROD", "生产环境", "正式环境", "UAT 数据库：", "PROD:"])
def test_sit_parser_stops_before_production_section(tmp_path, heading):
    path = _write(
        tmp_path,
        f"""SIT
host=sit.example.test
name=sit_test
user=sit_user
{heading}
password=must-not-import
""",
    )
    with pytest.raises(ValueError, match="incomplete"):
        read_sit_database(path)


@pytest.mark.parametrize(
    "content",
    [
        "UAT\nhost=uat.example.test\nname=uat_test\nuser=uat_user\npassword=test\n",
        "SIT\nSIT\nhost=sit.example.test\nname=sit_test\nuser=sit_user\npassword=test\n",
        "SIT\nhost=sit.example.test\nname=sit_test\nuser=sit_user\npassword=test\nhost=other.example.test\n",
    ],
)
def test_sit_parser_rejects_ambiguous_or_missing_sit(tmp_path, content):
    with pytest.raises(ValueError):
        read_sit_database(_write(tmp_path, content))


@pytest.mark.parametrize("ports", ["host=sit.example.test:5432\nport=6432", "host=sit.example.test\nport=0"])
def test_sit_parser_rejects_invalid_or_conflicting_ports(tmp_path, ports):
    with pytest.raises(ValueError):
        read_sit_database(_write(tmp_path, f"SIT\n{ports}\nname=sit_test\nuser=sit_user\npassword=test\n"))


def test_explicit_uat_reader_uses_office_database_not_vpc_or_app_credentials(tmp_path):
    path = _write(
        tmp_path,
        """SIT 数据库
host=sit.example.test
name=sit_test
user=sit_user
password=sit-secret
UAT 数据库
数据库连接地址(办公室访问)：uat-office.example.test:6432
VPC配置地址：uat-vpc.example.test:5432
数据库名称：uat_test
用户名：uat_user
密码：synthetic-uat-secret

供应商
password=must-not-read
UAT 应用服务器
host=app.example.test
user=app_user
password=must-not-read
""",
    )
    assert read_test_database(path, "UAT") == {
        "host": "uat-office.example.test",
        "port": 6432,
        "name": "uat_test",
        "user": "uat_user",
        "password": "synthetic-uat-secret",
    }


@pytest.mark.parametrize(
    "content",
    [
        "SIT\nhost=sit.test\nname=sit\nuser=u\npassword=p\n",
        "UAT 数据库\nhost=one.test\nUAT 数据库\nhost=two.test\nname=n\nuser=u\npassword=p\n",
        "UAT 数据库\nVPC配置地址=vpc.test\nname=n\nuser=u\npassword=p\n",
        "UAT 数据库\nhost=uat.test\nname=n\nuser=u\nUIT\npassword=must-not-read\n",
    ],
)
def test_uat_reader_fails_closed_for_missing_ambiguous_or_vpc_only_config(tmp_path, content):
    with pytest.raises(ValueError):
        read_test_database(_write(tmp_path, content), "UAT")


def test_remote_reader_rejects_non_test_environment(tmp_path):
    with pytest.raises(ValueError, match="Only explicit SIT or UAT"):
        read_test_database(_write(tmp_path, "PROD\nhost=prod.test\n"), "PROD")
