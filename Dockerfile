# One pinned Node major for BOTH the dashboard build stage and the runtime
# image, so native modules (better-sqlite3, sqlite3) built in the first load in
# the second by construction. Both bases are Debian bookworm (same glibc).
ARG NODE_MAJOR=22

# The dashboard is karakos-dashboard at the ref pinned in dashboard.ref. Its
# source is private and never committed here: bin/fetch-dashboard.sh (token)
# puts the verified source tarball in vendor/, or a release bundle
# (karakos-dashboard-bundle-<sha12>.tar.gz, no token) is dropped there. This
# stage does no network fetch of the dashboard. See bin/dashboard-stage.sh.
FROM node:${NODE_MAJOR}-bookworm-slim AS dashboard-build
# Empty means "the ref in dashboard.ref". When set, the input must be that commit.
ARG DASHBOARD_REF=
# Compiler toolchain for native modules when no prebuild matches; this stage is
# discarded, nothing here reaches the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ git \
    && rm -rf /var/lib/apt/lists/*
# `vendor*` may match nothing (no tarball): the stage script then fails with a clear message.
COPY dashboard.ref dashboard.ref.sha256 dashboard.bundle.sha256 vendor* /in/
COPY bin/dashboard-stage.sh /usr/local/bin/dashboard-stage.sh
RUN DASHBOARD_REF="${DASHBOARD_REF}" sh /usr/local/bin/dashboard-stage.sh /in /out

# The package's own files, minus the dashboard inputs: keeps the (private)
# source tarball and bundle out of the runtime image's layers.
FROM node:${NODE_MAJOR}-bookworm-slim AS workspace-src
COPY . /src
RUN rm -rf /src/vendor /src/dashboard.ref /src/dashboard.ref.sha256 /src/dashboard.bundle.sha256

FROM python:3.11-slim-bookworm

# Re-declare (an ARG before the first FROM is not visible inside a stage).
ARG NODE_MAJOR
ARG DASHBOARD_REF=
LABEL org.karakos.dashboard-ref="${DASHBOARD_REF}"

# Install runtime tools (tini for PID 1, git/curl/jq for runtime use)
# build-essential is needed because some Python deps (PyStemmer via fastembed)
# ship no aarch64 wheel and have to compile from source. We purge it after
# pip install to keep the final image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl tini jq build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install supervisord
RUN pip install --no-cache-dir supervisor

# Install Node.js for Claude CLI and the dashboard (same major as the build stage)
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Purge build toolchain after pip is done with it — keeps image lean
RUN apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user. The Claude Code CLI refuses
# `--dangerously-skip-permissions` when the running uid is 0, so the agent
# subprocess MUST run as a non-root user.  uid 1000 is a common host uid which
# makes bind-mounted credentials (~/.claude) line up cleanly for most users.
ARG KARAKOS_UID=1000
ARG KARAKOS_GID=1000
RUN groupadd --system --gid ${KARAKOS_GID} karakos \
    && useradd --system --uid ${KARAKOS_UID} --gid ${KARAKOS_GID} \
        --home-dir /home/karakos --create-home --shell /bin/bash karakos

# Bake the embedding model weights (as the build user) so a container with no
# network still has them. KARAKOS_BAKE_EMBED_MODEL=0 skips the download; recall
# then falls back to keyword mode.
ARG KARAKOS_BAKE_EMBED_MODEL=1
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN install -d -o karakos -g karakos /opt/fastembed
USER karakos
RUN if [ "$KARAKOS_BAKE_EMBED_MODEL" = "1" ]; then \
        python3 -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')"; \
    fi
USER root

WORKDIR /workspace
# WORKDIR creates the directory as root. Hand it to karakos so the user can
# write into it (entrypoint.sh runs `git init` there, agents log to it, etc.).
RUN chown karakos:karakos /workspace

# Use --chown on COPY rather than a post-hoc `chown -R` so the workspace's
# millions of node_modules files don't have to be rewritten in a new layer.
COPY --chown=karakos:karakos --from=dashboard-build /out/.next dashboard/.next
COPY --chown=karakos:karakos --from=dashboard-build /out/node_modules dashboard/node_modules
COPY --chown=karakos:karakos --from=dashboard-build /out/public dashboard/public
COPY --chown=karakos:karakos --from=dashboard-build /out/package.json dashboard/package.json
COPY --chown=karakos:karakos --from=dashboard-build /out/.dashboard-ref dashboard/.dashboard-ref
# next.config.{mjs,js,ts}: whichever the build stage kept (next start reads it).
COPY --chown=karakos:karakos --from=dashboard-build /out/next.config.* dashboard/
COPY --chown=karakos:karakos --from=workspace-src /src/ ./

# Dashboard runtime config (names fixed in the dashboard's package-env-mapping).
# The dashboard shares this container and reads agent-server.db from the data volume.
ENV KARAKOS_PROFILE=package \
    KARAKOS_REGISTRY_PATH=/workspace/config/agents.yaml \
    AGENT_SERVER_DB_PATH=/workspace/data/memory/agent-server.db \
    WORKSPACE_ROOT=/workspace

# Create data directories owned by karakos so volume mounts get the right
# ownership when first created.
RUN install -d -o karakos -g karakos \
        data data/messages data/memory data/health \
        logs logs/agent-streams logs/session-summaries \
        inbox

# Strip CRLF line endings from shell scripts. Belt-and-suspenders for Windows
# checkouts where git's autocrlf may have rewritten LF -> CRLF, which breaks
# shebangs inside the Linux container (env: 'bash\r': No such file, exit 127).
# .gitattributes also forces LF on these files at the repo level.
RUN find /workspace/bin -type f \( -name '*.sh' -o -name 'kara' \) \
        -exec sed -i 's/\r$//' {} +

USER karakos
ENTRYPOINT ["/usr/bin/tini", "--", "/workspace/bin/entrypoint.sh"]
