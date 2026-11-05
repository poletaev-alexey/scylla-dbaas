"""Building a scylla-driver session for a DBaaS Cluster.

* Token-aware routing on top of DC-aware round robin, so each request goes straight to a
  replica in the local datacenter.
* QUORUM by default: with RF=3 the Cluster keeps serving reads and writes while one Data
  Node is down (a Degraded Cluster still serves data at QUORUM, see GLOSSARY).
* Optional private->public address translation: Data Nodes may advertise private IPs to the
  driver; from the operator's laptop only the public ones are reachable (ADR-0004).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PORT = 9042


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ConnectionSettings:
    hosts: list[str]
    username: str | None = None
    password: str | None = None
    port: int = DEFAULT_PORT
    dc: str | None = None  # None: auto-detect from the Cluster topology
    address_map: dict[str, str] = field(default_factory=dict)
    connect_timeout: float = 15.0
    request_timeout: float = 30.0

    def validate(self) -> None:
        if not self.hosts:
            raise ConfigError("no contact points: pass --hosts or --from-api")
        if self.username and not self.password:
            raise ConfigError(
                "no password: the API reveals it only once; pass --password or CQL_PASSWORD"
            )


class MappingAddressTranslator:
    """Duck-typed ``cassandra.policies.AddressTranslator``: rewrite known private IPs."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = dict(mapping)

    def translate(self, addr: str) -> str:
        return self.mapping.get(addr, addr)


def parse_hosts(raw: str | Iterable[str]) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [h.strip() for h in items if h.strip()]


def pick_datacenter(datacenters: Iterable[str | None], override: str | None) -> str:
    """The DC name used in the keyspace replication map.

    It depends on the snitch: Ec2Snitch on eu-central-1 reports ``eu-central``; a local
    Docker Cluster reports ``datacenter1``. Reading it from the live topology avoids
    hard-coding either.
    """
    if override:
        return override
    found = sorted({dc for dc in datacenters if dc})
    if len(found) != 1:
        raise ConfigError(
            f"cannot pick a datacenter automatically (found {found or 'none'}); pass --dc"
        )
    return found[0]


def build_cluster(cfg: ConnectionSettings) -> Any:
    """Return an unconnected ``cassandra.cluster.Cluster``."""
    from cassandra import ConsistencyLevel
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile
    from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy

    cfg.validate()
    # With local_dc unset the driver takes the DC of the contact points.
    lbp = TokenAwarePolicy(DCAwareRoundRobinPolicy(local_dc=cfg.dc or ""))
    profile = ExecutionProfile(
        load_balancing_policy=lbp,
        consistency_level=ConsistencyLevel.QUORUM,
        request_timeout=cfg.request_timeout,
    )
    kwargs: dict[str, Any] = {
        "contact_points": cfg.hosts,
        "port": cfg.port,
        "execution_profiles": {EXEC_PROFILE_DEFAULT: profile},
        "connect_timeout": cfg.connect_timeout,
    }
    if cfg.username:
        kwargs["auth_provider"] = PlainTextAuthProvider(
            username=cfg.username, password=cfg.password
        )
    if cfg.address_map:
        kwargs["address_translator"] = MappingAddressTranslator(cfg.address_map)
    return Cluster(**kwargs)


def cluster_datacenters(cluster: Any) -> list[str]:
    return [h.datacenter for h in cluster.metadata.all_hosts()]
