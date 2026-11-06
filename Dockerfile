# Runtime image for Document Desk: web (default), worker, migrate and setup-analyzers are commands of one image.
#   docker build -t docdesk .
#   docker run --rm docdesk web | worker | migrate | setup-analyzers [--check|--set-defaults] | smoke ...
# No secrets or tenant identifiers are baked in; configuration is environment variables and Key Vault (docs/deployment.md).
FROM python:3.12-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim-bookworm
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/opt/venv/bin:$PATH
# Microsoft ODBC Driver 18 for SQL Server (EULA accepted by ACCEPT_EULA=Y) and the unixODBC runtime
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
COPY migrations /app/migrations
RUN useradd --system --uid 10001 --no-create-home docdesk
USER 10001
WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["docdesk"]
CMD ["web"]
