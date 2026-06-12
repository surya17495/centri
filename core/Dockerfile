# CENTRI core/server — multi-arch (linux/arm64 + linux/amd64).
# Plain python slim base, no arch-specific binaries, so the same image
# definition builds on an Oracle Ampere A1 (arm64) or an x86_64 box.
FROM python:3.11-slim

# git/curl: git is used by the coding hands; curl is handy for in-container
# health checks. Kept minimal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (cached layer): copy only the package metadata, then the
# source. `pip install .` reads dependencies from pyproject.toml.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[embeddings]'

# Persistent state lives here; compose mounts a volume over it.
ENV CENTRI_DB_PATH=/data/state.db
# Bind to all interfaces inside the container; the host controls exposure via
# the compose port mapping + the VM firewall.
ENV CENTRI_CORE_HOST=0.0.0.0
ENV CENTRI_CORE_PORT=8760
RUN mkdir -p /data

EXPOSE 8760

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8760/health || exit 1

# centri = uvicorn entry point (see core/pyproject.toml [project.scripts]).
CMD ["centri"]
