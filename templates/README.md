# Templates

This directory contains template files that `swarm init` copies or processes when setting up a new project.

## Files

### CLAUDE.md
Static template copied directly to the target project root. Contains the agent operating manual - instructions for autonomous agents on how to use the ticket system.

### docker-compose.yml
Template for the generated docker-compose file. Contains:
- `# __AGENT_SERVICES__` marker where agent service blocks are inserted
- `__MONITOR_PORT__` placeholder for the monitor port from config

### agent-service.yml
Template for a single agent service block. Duplicated N times based on `config.agents`. Placeholders:
- `__N__` - agent number (1, 2, 3...)
- `__MAX_TURNS__` - from config.json
- `__ALLOWED_TOOLS__` - from config.json
- `__NTFY_TOPIC_LINE__` - entire line inserted if ntfy_topic is configured, otherwise removed

## How generation works

`generate_docker_compose()` in `swarm/swarm.py`:
1. Reads both template files
2. Duplicates agent-service.yml N times with substitutions
3. Inserts the agent blocks at the `# __AGENT_SERVICES__` marker
4. Substitutes `__MONITOR_PORT__`

This keeps most of the docker-compose structure in editable template files while allowing dynamic agent count.
