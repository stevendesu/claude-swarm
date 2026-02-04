# Agent container for the autonomous agent swarm.
# Build context must be the project root (see docker-compose.yml).

FROM node:lts-slim

# ── System dependencies ──────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git jq curl python3 && \
    rm -rf /var/lib/apt/lists/*

# ── Claude Code CLI ──────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── Ticket CLI ───────────────────────────────────────────────────────────
COPY ticket/ticket.py /usr/local/lib/ticket.py
RUN ln -s /usr/local/lib/ticket.py /usr/local/bin/ticket && \
    chmod +x /usr/local/lib/ticket.py

# ── Agent loop + entrypoint ──────────────────────────────────────────────
COPY agent/agent-loop.sh /usr/local/bin/agent-loop.sh
COPY agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/agent-loop.sh /usr/local/bin/entrypoint.sh

# ── Defaults ─────────────────────────────────────────────────────────────
ENV AGENT_ID=""
ENV NTFY_TOPIC=""
ENV MAX_TURNS="50"
ENV ALLOWED_TOOLS="Bash,Read,Write,Edit,Glob,Grep"

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
