# ScyllaDB + Scylla Manager Agent in one container: a local Data Node.
# Pattern from the Scylla Manager Docker docs (https://manager.docs.scylladb.com/stable/docker/).
# The agent config (auth token, S3/MinIO credentials) is copied in per Cluster by the
# `local` Provider driver before the container starts.
# Built by dbaas.providers.local from stdin (no build context needed).
ARG SCYLLA_IMAGE=scylladb/scylla:2026.1.15
ARG AGENT_IMAGE=scylladb/scylla-manager-agent:3.13.0

FROM ${AGENT_IMAGE} AS agent
FROM ${SCYLLA_IMAGE}

COPY --from=agent /usr/bin/scylla-manager-agent /usr/bin/

USER root
RUN mkdir -p /etc/scylla-manager-agent \
 && printf '%s\n' \
      '[program:scylla-manager-agent]' \
      'command=/usr/bin/scylla-manager-agent' \
      'autorestart=true' \
      'stdout_logfile=/dev/stdout' \
      'stdout_logfile_maxbytes=0' \
      'stderr_logfile=/dev/stderr' \
      'stderr_logfile_maxbytes=0' \
      > /etc/supervisord.conf.d/scylla-manager-agent.conf
USER scylla
