# Control Plane image (ADR-0001). Build context: repository root
#   docker build -f control-plane/Dockerfile -t dbaas-control-plane .
FROM hashicorp/terraform:1.9.8 AS terraform
FROM docker:28-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# terraform for the aws driver, docker CLI for the local driver.
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

RUN useradd --create-home --uid 1000 dbaas
WORKDIR /app/control-plane

COPY control-plane/pyproject.toml ./
COPY control-plane/src ./src
RUN pip install ".[aws]"

# Assets the drivers read at runtime (paths relative to /app/control-plane).
COPY infra /app/infra
COPY deploy/local /app/deploy/local

ENV DBAAS_DATABASE_URL=sqlite+aiosqlite:////data/dbaas.db \
    DBAAS_TERRAFORM_DIR=/app/infra/cluster \
    DBAAS_LOCAL_DOCKERFILE=/app/deploy/local/scylla-agent/Dockerfile
RUN mkdir /data && chown dbaas /data
VOLUME /data
USER dbaas

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=2)"
CMD ["uvicorn", "dbaas.main:app", "--host", "0.0.0.0", "--port", "8000"]
