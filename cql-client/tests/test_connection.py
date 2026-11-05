import pytest
from cassandra import ConsistencyLevel
from cassandra.cluster import EXEC_PROFILE_DEFAULT
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy

from cql_client.connection import (
    ConfigError,
    ConnectionSettings,
    MappingAddressTranslator,
    build_cluster,
    parse_hosts,
    pick_datacenter,
)


def test_parse_hosts() -> None:
    assert parse_hosts(" a, b ,,c") == ["a", "b", "c"]
    assert parse_hosts("") == []


@pytest.mark.parametrize(
    ("dcs", "override", "expected"),
    [
        (["eu-central", "eu-central", "eu-central"], None, "eu-central"),
        (["datacenter1", None], None, "datacenter1"),
        (["a", "b"], "b", "b"),
    ],
)
def test_pick_datacenter(dcs: list[str | None], override: str | None, expected: str) -> None:
    assert pick_datacenter(dcs, override) == expected


def test_pick_datacenter_ambiguous() -> None:
    with pytest.raises(ConfigError):
        pick_datacenter(["a", "b"], None)
    with pytest.raises(ConfigError):
        pick_datacenter([], None)


def test_address_translator() -> None:
    t = MappingAddressTranslator({"10.0.1.1": "203.0.113.1"})
    assert t.translate("10.0.1.1") == "203.0.113.1"
    assert t.translate("10.0.1.9") == "10.0.1.9"


def test_settings_require_password_with_username() -> None:
    with pytest.raises(ConfigError, match="password"):
        ConnectionSettings(hosts=["h"], username="scylla").validate()
    with pytest.raises(ConfigError, match="contact points"):
        ConnectionSettings(hosts=[]).validate()


def test_build_cluster_is_token_aware_quorum_and_translates() -> None:
    cfg = ConnectionSettings(
        hosts=["203.0.113.1"],
        username="scylla",
        password="pw",
        dc="eu-central",
        address_map={"10.0.1.1": "203.0.113.1"},
    )
    cluster = build_cluster(cfg)  # not connected: no network I/O
    profile = cluster.profile_manager.profiles[EXEC_PROFILE_DEFAULT]
    assert profile.consistency_level == ConsistencyLevel.QUORUM
    lbp = profile.load_balancing_policy
    assert isinstance(lbp, TokenAwarePolicy)
    assert isinstance(lbp._child_policy, DCAwareRoundRobinPolicy)
    assert lbp._child_policy.local_dc == "eu-central"
    assert cluster.address_translator.translate("10.0.1.1") == "203.0.113.1"
    assert cluster.auth_provider.username == "scylla"
