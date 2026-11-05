import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeSession

from cql_client import cli
from cql_client.api import DbaasApi


def _run(connector: Any, tmp_path: Path, *args: str) -> int:
    base = ["--hosts", "h1,h2", "--manifest", str(tmp_path / "m.json")]
    return cli.main([*base, *args], connector=connector)


def test_write_then_verify(
    connector: Any, session: FakeSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(connector, tmp_path, "write", "25") == 0
    assert len(session.rows) == 25
    assert len(json.loads((tmp_path / "m.json").read_text())["rows"]) == 25
    assert _run(connector, tmp_path, "verify") == 0
    assert "verified 25 rows: 0 missing, 0 mismatched" in capsys.readouterr().out
    assert connector.seen[0].hosts == ["h1", "h2"]


def test_verify_exits_nonzero_on_mismatch(
    connector: Any, session: FakeSession, tmp_path: Path
) -> None:
    assert _run(connector, tmp_path, "write", "3") == 0
    session.rows.clear()  # e.g. a Restore that lost data
    assert _run(connector, tmp_path, "verify") == 1


def test_verify_without_manifest_is_usage_error(connector: Any, tmp_path: Path) -> None:
    assert _run(connector, tmp_path, "verify") == 2


def test_demo_and_read(connector: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(connector, tmp_path, "demo", "--rows", "4") == 0
    out = capsys.readouterr().out
    assert out.count("row-") == 4
    assert _run(connector, tmp_path, "read", "--limit", "2") == 0
    assert capsys.readouterr().out.count("row-") == 2


def test_missing_hosts_is_usage_error(connector: Any, tmp_path: Path) -> None:
    assert cli.main(["--hosts", "", "read"], connector=connector) == 2


def _api(password: str | None) -> DbaasApi:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["X-API-Key"] == "k"
        if req.url.path == "/api/v1/clusters/c-1/connection":
            return httpx.Response(
                200,
                json={
                    "contact_points": ["203.0.113.1", "203.0.113.2"],
                    "port": 9042,
                    "username": "scylla",
                    "password": password,
                    "cql_client_command": "…",
                },
            )
        if req.url.path == "/api/v1/clusters/c-1":
            return httpx.Response(
                200,
                json={
                    "nodes": [
                        {"role": "data", "private_ip": "10.0.1.1", "public_ip": "203.0.113.1"},
                        {"role": "monitor", "private_ip": "10.0.1.10", "public_ip": "x"},
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "nope"})

    return DbaasApi("http://cp", "k", transport=httpx.MockTransport(handler))


def _api_args(*extra: str) -> Any:
    return cli.build_parser().parse_args(
        ["--from-api", "http://cp", "--cluster", "c-1", "--api-key", "k", *extra, "read"]
    )


def test_from_api_first_call_uses_revealed_password() -> None:
    cfg = cli.resolve_settings(_api_args(), api=_api("s3cret"))
    assert cfg.hosts == ["203.0.113.1", "203.0.113.2"]
    assert (cfg.username, cfg.password) == ("scylla", "s3cret")
    assert cfg.address_map == {"10.0.1.1": "203.0.113.1"}


def test_from_api_later_call_needs_password_flag() -> None:
    cfg = cli.resolve_settings(_api_args(), api=_api(None))
    with pytest.raises(cli.ConfigError, match="password"):
        cfg.validate()
    cfg = cli.resolve_settings(_api_args("--password", "pw"), api=_api(None))
    cfg.validate()
    assert cfg.password == "pw"


def test_from_api_requires_cluster_and_key() -> None:
    args = cli.build_parser().parse_args(["--from-api", "http://cp", "read"])
    args.cluster = args.api_key = None
    with pytest.raises(cli.ConfigError):
        cli.resolve_settings(args)
