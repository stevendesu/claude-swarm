#!/usr/bin/env python3
"""swarm — Bootstrap and lifecycle management for autonomous agent swarms.

Usage:
    swarm init /path/to/project    Initialize a project for agent swarms
    swarm start                     Spin up agent containers + monitor
    swarm stop                      Shut down all agents and monitor
    swarm status                    Show running agents and queue summary
    swarm logs <agent-name>         Tail logs for a specific agent
    swarm scale N                   Adjust number of agent containers
    swarm regenerate                Regenerate docker-compose.yml and .swarm/ files
"""

import argparse
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time

# ---------------------------------------------------------------------------
# Paths — where the swarm package itself lives (for copying agent/, ticket/, etc.)
# ---------------------------------------------------------------------------

SWARM_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT_OF_SWARM = os.path.dirname(SWARM_SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

CLAUDE_MD_TEMPLATE = """\
# Agent Operating Manual

This file guides autonomous AI agents working on this project.
Read PROJECT.md for business context and product vision.

## Ticket CLI Reference

All commands use `ticket --db /tickets/tickets.db`.

| Action | Command |
|--------|---------|
| Claim work | `claim-next --agent $AGENT_ID` |
| Log progress | `comment <ID> "message" --author $AGENT_ID` |
| Break down work | `create "Sub-task" --parent <ID> --created-by $AGENT_ID` |
| Set dependency | `create "Later task" --blocks <EARLIER_ID> --created-by $AGENT_ID` |
| Mark blocked | `block <ID> --by <BLOCKER_ID>` (auto-releases the ticket) |
| Ask humans | `create "Question" --assign human --blocks <ID> --created-by $AGENT_ID` |
| Release if stuck | `unclaim <ID>` |
| Finish work | `complete <ID>` |

## Dependencies

Use `--blocks` when creating tickets to establish dependency order.
Earlier/foundational tickets should block later/dependent ones.

If your current ticket depends on unfinished work, run `ticket block <ID> --by <BLOCKER_ID>` — \
this automatically releases the ticket back to the pool. Once the blocker is done, \
the ticket becomes claimable again.

## Decision Making

- **Technical decisions** (database, framework, architecture): Make the call, document in a comment
- **Business decisions** (users, monetization, direction): Create a human-assigned blocking ticket
"""

PROJECT_MD_TEMPLATE = """\
# Project Context

## What does this product do?
[Describe your product]

## Who are the target users?
[Describe your users]

## Business model / problem solved
[How does it make money or what problem does it solve?]

## Hard constraints
[Regulatory, platform, timeline constraints]

## What does success look like?
[Define success criteria]
"""

INTERVIEW_SYSTEM_PROMPT = """\
You are conducting a short interview to understand a new project's business context. \
Your goal is to populate PROJECT.md with what you learn.

Ask about these topics (one or two at a time, conversationally):
1. What does this product do?
2. Who are the target users?
3. How will it make money, or what problem does it solve?
4. Are there hard constraints (regulatory, platform, timeline)?
5. What does success look like?

Guidelines:
- Be conversational and brief. Don't overwhelm with questions.
- It's fine to combine or skip questions based on what the human volunteers.
- When you have enough context, write PROJECT.md at the project root using the Write tool.
- Keep PROJECT.md concise — a few clear paragraphs, not an essay.
- Use the same five section headings that already exist in the placeholder PROJECT.md.
- Do NOT make technical decisions (database, framework, architecture). Agents decide those.
- After writing PROJECT.md, call the mcp__interview__end_interview tool to automatically end the session. \
Do NOT use the Skill tool — use the MCP tool directly.
"""

DEFAULT_CONFIG = {
    "agents": 3,
    "ntfy_topic": "",
    "allowed_tools": "Bash,Read,Write,Edit,Glob,Grep",
    "max_turns": 50,
    "monitor_port": 3000,
}

SEED_TICKET_TITLE = (
    "Decompose project into work tickets"
)

SEED_TICKET_DESCRIPTION = (
    "Read PROJECT.md to understand the product vision and CLAUDE.md for operating guidelines. "
    "Then break the project into concrete, actionable tickets using the ticket CLI.\n\n"
    "IMPORTANT — establish dependency order using the --blocks flag:\n"
    "- Earlier/foundational tickets should block later/dependent ones\n"
    "- Example: 'Setup project structure' (ticket #2) should block 'Implement user auth' (ticket #3):\n"
    "    ticket create 'Setup project structure' --parent 1 --created-by $AGENT_ID\n"
    "    ticket create 'Implement user auth' --parent 1 --blocks 2 --created-by $AGENT_ID\n\n"
    "Each ticket should be small enough for one agent to complete in a single session. "
    "Include clear descriptions so any agent can pick up the work."
)


# ---------------------------------------------------------------------------
# docker-compose.yml generator
# ---------------------------------------------------------------------------

def generate_docker_compose(config):
    """Return a docker-compose.yml string based on the given config dict.

    The generated file is placed at the PROJECT root and all paths are
    relative to the project root (i.e. the directory that contains .swarm/).
    """
    agent_count = config.get("agents", 3)
    monitor_port = config.get("monitor_port", 3000)
    ntfy_topic = config.get("ntfy_topic", "")
    max_turns = config.get("max_turns", 50)
    allowed_tools = config.get("allowed_tools", "Bash,Read,Write,Edit,Glob,Grep")

    lines = ["services:"]

    for i in range(1, agent_count + 1):
        name = f"agent-{i}"
        lines.append(f"  {name}:")
        lines.append("    build:")
        lines.append("      context: .swarm")
        lines.append("      dockerfile: agent/Dockerfile")
        lines.append("    volumes:")
        lines.append("      - ./.swarm/tickets:/tickets")
        lines.append("      - ./.swarm/repo.git:/repo.git")
        lines.append("      - ${HOME}/.claude:/host-claude-config:ro")
        lines.append("    environment:")
        lines.append(f"      - AGENT_ID={name}")
        lines.append("      - CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}")
        if ntfy_topic:
            lines.append(f"      - NTFY_TOPIC={ntfy_topic}")
        lines.append(f"      - MAX_TURNS={max_turns}")
        lines.append(f"      - ALLOWED_TOOLS={allowed_tools}")
        lines.append("    restart: unless-stopped")
        lines.append("")

    # Monitor service — only include if monitor/ was copied into .swarm/
    lines.append("  monitor:")
    lines.append("    build: ./.swarm/monitor")
    lines.append("    ports:")
    lines.append('      - "${MONITOR_PORT:-' + str(monitor_port) + '}:3000"')
    lines.append("    volumes:")
    lines.append("      - ./.swarm/tickets:/tickets")
    lines.append("      - /var/run/docker.sock:/var/run/docker.sock:ro")
    lines.append("    restart: unless-stopped")
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Config helpers (JSON-based, no YAML dependency)
# ---------------------------------------------------------------------------

def load_config(project_dir):
    """Load .swarm/config.json from the given project directory."""
    config_path = os.path.join(project_dir, ".swarm", "config.json")
    if not os.path.isfile(config_path):
        print(f"Error: {config_path} not found. Run 'swarm init' first.", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "r") as f:
        return json.load(f)


def save_config(project_dir, config):
    """Write config dict to .swarm/config.json."""
    config_path = os.path.join(project_dir, ".swarm", "config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Locate project root (walk up from cwd looking for .swarm/)
# ---------------------------------------------------------------------------

def find_project_dir():
    """Find the project root by looking for .swarm/ in cwd or parents."""
    d = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(d, ".swarm")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    print("Error: No .swarm/ directory found. Run 'swarm init /path/to/project' first.",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Initialize a project directory for agent swarms."""
    project_dir = os.path.abspath(args.project_dir)

    if not os.path.isdir(project_dir):
        os.makedirs(project_dir)
        print(f"Created {project_dir}")

    swarm_dir = os.path.join(project_dir, ".swarm")
    if os.path.isdir(swarm_dir):
        print(f"Warning: {swarm_dir} already exists. Re-initializing will overwrite config.")
        resp = input("Continue? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            sys.exit(0)

    print(f"Initializing swarm in {project_dir} ...")

    # ── 1. Create directory structure ────────────────────────────────────
    os.makedirs(os.path.join(swarm_dir, "agent"), exist_ok=True)
    os.makedirs(os.path.join(swarm_dir, "ticket"), exist_ok=True)
    os.makedirs(os.path.join(swarm_dir, "tickets"), exist_ok=True)  # bind-mount dir for tickets.db
    # ── 2. Copy agent/ files ────────────────────────────────────────────
    agent_src = os.path.join(PROJECT_ROOT_OF_SWARM, "agent")
    if os.path.isdir(agent_src):
        for fname in ("agent-loop.sh", "entrypoint.sh", "Dockerfile"):
            src = os.path.join(agent_src, fname)
            dst = os.path.join(swarm_dir, "agent", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                print(f"  Copied agent/{fname}")
    else:
        print(f"  Warning: {agent_src} not found — skipping agent file copy.")

    # ── 3. Copy ticket/ files ───────────────────────────────────────────
    ticket_src = os.path.join(PROJECT_ROOT_OF_SWARM, "ticket")
    if os.path.isdir(ticket_src):
        for fname in os.listdir(ticket_src):
            src = os.path.join(ticket_src, fname)
            dst = os.path.join(swarm_dir, "ticket", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                print(f"  Copied ticket/{fname}")
    else:
        print(f"  Warning: {ticket_src} not found — skipping ticket file copy.")

    # ── 4. Copy monitor/ if it exists ───────────────────────────────────
    monitor_src = os.path.join(PROJECT_ROOT_OF_SWARM, "monitor")
    monitor_dst = os.path.join(swarm_dir, "monitor")
    if os.path.isdir(monitor_src):
        if os.path.isdir(monitor_dst):
            shutil.rmtree(monitor_dst)
        shutil.copytree(monitor_src, monitor_dst)
        print("  Copied monitor/")
    else:
        print("  Note: monitor/ not found — monitor service will not be available.")
        # Create a placeholder so docker-compose doesn't break at build time
        os.makedirs(monitor_dst, exist_ok=True)
        placeholder_dockerfile = os.path.join(monitor_dst, "Dockerfile")
        if not os.path.isfile(placeholder_dockerfile):
            with open(placeholder_dockerfile, "w") as f:
                f.write("FROM alpine:latest\nCMD [\"echo\", \"Monitor not yet built\"]\n")

    # ── 5. Write default config.json ────────────────────────────────────
    config = dict(DEFAULT_CONFIG)
    save_config(project_dir, config)
    print("  Created config.json")

    # ── 6. Initialize SQLite database ───────────────────────────────────
    tickets_db_path = os.path.join(swarm_dir, "tickets", "tickets.db")
    ticket_py = os.path.join(swarm_dir, "ticket", "ticket.py")
    if os.path.isfile(ticket_py):
        # Run 'ticket list' which triggers auto-init of the schema
        subprocess.run(
            [sys.executable, ticket_py, "--db", tickets_db_path, "list"],
            capture_output=True,
        )
        print("  Initialized tickets.db")
    else:
        print("  Warning: ticket.py not found — database not initialized.")

    # ── 7. Update .gitignore (before git init so .swarm/ is never tracked)
    gitignore_path = os.path.join(project_dir, ".gitignore")
    entries_to_add = [".swarm/", ".agent-logs/"]
    existing_lines = set()
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, "r") as f:
            existing_lines = set(line.strip() for line in f)

    with open(gitignore_path, "a") as f:
        for entry in entries_to_add:
            if entry not in existing_lines:
                f.write(f"{entry}\n")
    print("  Updated .gitignore")

    # ── 8. Generate docker-compose.yml at project root ──────────────────
    compose_path = os.path.join(project_dir, "docker-compose.yml")
    compose_content = generate_docker_compose(config)
    with open(compose_path, "w") as f:
        f.write(compose_content)
    print("  Generated docker-compose.yml")

    # ── 9. Generate CLAUDE.md at project root ───────────────────────────
    claude_md_path = os.path.join(project_dir, "CLAUDE.md")
    if not os.path.isfile(claude_md_path):
        with open(claude_md_path, "w") as f:
            f.write(CLAUDE_MD_TEMPLATE)
        print("  Generated CLAUDE.md")
    else:
        print("  CLAUDE.md already exists — skipping")

    # ── 10. Generate PROJECT.md placeholder at project root ─────────────
    project_md_path = os.path.join(project_dir, "PROJECT.md")
    if not os.path.isfile(project_md_path):
        with open(project_md_path, "w") as f:
            f.write(PROJECT_MD_TEMPLATE)
        print("  Generated PROJECT.md (placeholder — edit this file!)")
    else:
        print("  PROJECT.md already exists — skipping")

    # ── 11. Create bare git repo ──────────────────────────────────────────
    # NOTE: This must come AFTER generating docker-compose.yml, CLAUDE.md,
    # and PROJECT.md so they are included in the initial commit and visible
    # to agents when they clone the repo.
    bare_repo_path = os.path.join(swarm_dir, "repo.git")
    if os.path.isdir(bare_repo_path):
        shutil.rmtree(bare_repo_path)

    # Check if the project is a git repo
    git_check = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    )
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "swarm", "GIT_AUTHOR_EMAIL": "swarm@local",
               "GIT_COMMITTER_NAME": "swarm", "GIT_COMMITTER_EMAIL": "swarm@local"}
    if git_check.returncode == 0:
        # Project is already a git repo — stage and commit new files before bare-cloning
        subprocess.run(["git", "-C", project_dir, "add", "docker-compose.yml", "CLAUDE.md",
                        "PROJECT.md", ".gitignore"], capture_output=True)
        subprocess.run(
            ["git", "-C", project_dir, "commit", "-m", "Add swarm config files (swarm init)"],
            capture_output=True, text=True, env=git_env,
        )
        subprocess.run(
            ["git", "clone", "--bare", project_dir, bare_repo_path],
            check=True, capture_output=True, text=True,
        )
        print("  Created bare git repo from project")
    else:
        # Project isn't a git repo — init one, make an initial commit, then bare-clone
        print("  Project is not a git repo. Initializing git ...")
        subprocess.run(["git", "-C", project_dir, "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", project_dir, "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", project_dir, "commit", "-m", "Initial commit (swarm init)"],
            check=True, capture_output=True, text=True, env=git_env,
        )
        subprocess.run(
            ["git", "clone", "--bare", project_dir, bare_repo_path],
            check=True, capture_output=True, text=True,
        )
        print("  Initialized git repo and created bare clone")

    # ── 12. Create seed ticket ──────────────────────────────────────────
    if os.path.isfile(ticket_py):
        result = subprocess.run(
            [sys.executable, ticket_py, "--db", tickets_db_path,
             "create", SEED_TICKET_TITLE,
             "--description", SEED_TICKET_DESCRIPTION,
             "--created-by", "swarm-init"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            seed_id = result.stdout.strip()
            print(f"  Created seed ticket #{seed_id}")
        else:
            print(f"  Warning: Failed to create seed ticket: {result.stderr.strip()}")

    # ── Phase 1 complete ────────────────────────────────────────────────
    print()
    print("Phase 1 complete — infrastructure scaffolded.")

    # ── Phase 2: Interactive project clarification ───────────────────
    claude_path = shutil.which("claude")
    phase2_ran = False

    if claude_path:
        print()
        print("── Phase 2: Interactive project clarification ──")
        print()
        print("Launching Claude Code to interview you about your project.")
        print("This will populate PROJECT.md with business context for the agents.")
        print()

        sentinel_path = os.path.join(swarm_dir, ".interview-done")
        if os.path.exists(sentinel_path):
            os.remove(sentinel_path)

        mcp_server_path = os.path.join(SWARM_SCRIPT_DIR, "interview-mcp.py")
        mcp_config_data = {
            "mcpServers": {
                "interview": {
                    "command": sys.executable,
                    "args": [mcp_server_path],
                    "env": {"INTERVIEW_SENTINEL": sentinel_path},
                }
            }
        }
        mcp_config_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="swarm-mcp-", delete=False
        )
        json.dump(mcp_config_data, mcp_config_file)
        mcp_config_file.close()

        proc = subprocess.Popen(
            [
                claude_path,
                "--system-prompt", INTERVIEW_SYSTEM_PROMPT,
                "--allowedTools", "Read,Write,Edit,Glob,Grep,mcp__interview__end_interview",
                "--mcp-config", mcp_config_file.name,
                "--",
                "Let's get started.",
            ],
            cwd=project_dir,
        )

        # Poll for the sentinel file written by the end_interview MCP tool.
        # Once detected, wait briefly for Claude to finish its final response,
        # then send SIGINT to end the session.
        while proc.poll() is None:
            if os.path.exists(sentinel_path):
                time.sleep(3)
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                break
            time.sleep(0.5)

        if proc.returncode is None:
            proc.wait()

        if os.path.exists(sentinel_path):
            os.remove(sentinel_path)
        os.unlink(mcp_config_file.name)

        # Commit updated PROJECT.md to the bare repo so agents see it
        subprocess.run(["git", "-C", project_dir, "add", "PROJECT.md"], capture_output=True)
        commit_result = subprocess.run(
            ["git", "-C", project_dir, "commit", "-m", "Update PROJECT.md from interview (swarm init)"],
            capture_output=True, text=True, env=git_env,
        )
        if commit_result.returncode == 0:
            # Detect the current branch and push to the bare repo
            branch_result = subprocess.run(
                ["git", "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True,
            )
            branch = branch_result.stdout.strip() or "main"
            subprocess.run(
                ["git", "-C", project_dir, "push", bare_repo_path, f"HEAD:{branch}"],
                capture_output=True, text=True,
            )
            print("  Updated bare repo with PROJECT.md from interview")

        phase2_ran = True
    else:
        print()
        print("Note: Claude Code CLI ('claude') not found on PATH.")
        print("  Skipping interactive project clarification.")

    # ── Done ────────────────────────────────────────────────────────────
    print()
    print("Swarm initialized successfully!")
    print()
    print("Next steps:")
    step = 1
    if not phase2_ran:
        print(f"  {step}. Edit {project_md_path}")
        print("     Fill in the business context so agents understand what to build.")
        print()
        step += 1
    print(f"  {step}. Start the swarm:")
    print(f"     cd {project_dir}")
    print("     swarm start")
    print()


def find_free_port(preferred):
    """Return *preferred* if available, otherwise scan upward for a free port."""
    for port in range(preferred, preferred + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred  # fall back; let docker surface the error


def extract_oauth_token():
    """Extract the Claude Code OAuth token from the macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        creds = json.loads(result.stdout.strip())
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, FileNotFoundError):
        return None


def cmd_start(args):
    """Spin up agent containers and monitor."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    compose_file = os.path.join(project_dir, "docker-compose.yml")

    if not os.path.isfile(compose_file):
        # Regenerate it from config
        compose_content = generate_docker_compose(config)
        with open(compose_file, "w") as f:
            f.write(compose_content)
        print("Regenerated docker-compose.yml from config.")

    preferred = config.get("monitor_port", 3000)
    actual = find_free_port(preferred)
    if actual != preferred:
        print(f"Port {preferred} is in use, using {actual} for monitor.")

    # Extract OAuth token from macOS Keychain if not already set
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not oauth_token:
        print("Extracting Claude Code OAuth token from Keychain ...")
        oauth_token = extract_oauth_token()
        if not oauth_token:
            print("Error: Could not extract OAuth token from macOS Keychain.", file=sys.stderr)
            print("  Either log in with 'claude' first, or set CLAUDE_CODE_OAUTH_TOKEN manually.",
                  file=sys.stderr)
            sys.exit(1)
        print("OAuth token extracted successfully.")

    env = {**os.environ, "MONITOR_PORT": str(actual), "CLAUDE_CODE_OAUTH_TOKEN": oauth_token}

    print("Starting swarm ...")
    print(f"Monitor will be available at http://localhost:{actual}")
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, "up", "-d", "--build"],
        cwd=project_dir,
        env=env,
    )
    sys.exit(result.returncode)


def cmd_stop(args):
    """Shut down all agents and monitor."""
    project_dir = find_project_dir()
    compose_file = os.path.join(project_dir, "docker-compose.yml")

    print("Stopping swarm ...")
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, "down"],
        cwd=project_dir,
    )
    sys.exit(result.returncode)


def cmd_status(args):
    """Show running agents and queue summary."""
    project_dir = find_project_dir()
    compose_file = os.path.join(project_dir, "docker-compose.yml")
    tickets_db = os.path.join(project_dir, ".swarm", "tickets", "tickets.db")
    ticket_py = os.path.join(project_dir, ".swarm", "ticket", "ticket.py")

    # ── Container status ────────────────────────────────────────────────
    print("=== Containers ===")
    print()
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "ps"],
        cwd=project_dir,
    )
    print()

    # ── Ticket queue summary ────────────────────────────────────────────
    if os.path.isfile(ticket_py) and os.path.isfile(tickets_db):
        print("=== Ticket Queue ===")
        print()
        for status_label, status_filter in [
            ("Open", "open"),
            ("In Progress", "in_progress"),
            ("Done", "done"),
        ]:
            result = subprocess.run(
                [sys.executable, ticket_py, "--db", tickets_db, "count", "--status", status_filter],
                capture_output=True, text=True,
            )
            count = result.stdout.strip() if result.returncode == 0 else "?"
            print(f"  {status_label:15s} {count}")

        # Count human-assigned tickets
        result = subprocess.run(
            [sys.executable, ticket_py, "--db", tickets_db, "list",
             "--assigned-to", "human", "--format", "json"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            try:
                human_tickets = json.loads(result.stdout)
                print(f"  {'Needs Human':15s} {len(human_tickets)}")
            except (json.JSONDecodeError, TypeError):
                pass

        print()
    else:
        print("Ticket database not found — run 'swarm init' first.")
        print()


def cmd_logs(args):
    """Tail logs for a specific agent."""
    project_dir = find_project_dir()
    compose_file = os.path.join(project_dir, "docker-compose.yml")

    service_name = args.service
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "logs", "-f", service_name],
            cwd=project_dir,
        )
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        sys.exit(0)


def cmd_scale(args):
    """Adjust the number of agent containers."""
    project_dir = find_project_dir()
    config = load_config(project_dir)

    new_count = args.count
    old_count = config.get("agents", 3)

    if new_count < 1:
        print("Error: Agent count must be at least 1.", file=sys.stderr)
        sys.exit(1)

    config["agents"] = new_count
    save_config(project_dir, config)
    print(f"Updated agent count: {old_count} -> {new_count}")

    # Regenerate docker-compose.yml
    compose_path = os.path.join(project_dir, "docker-compose.yml")
    compose_content = generate_docker_compose(config)
    with open(compose_path, "w") as f:
        f.write(compose_content)
    print("Regenerated docker-compose.yml")

    # Bring up the new configuration
    print("Applying new configuration ...")
    result = subprocess.run(
        ["docker", "compose", "-f", compose_path, "up", "-d", "--build"],
        cwd=project_dir,
    )
    sys.exit(result.returncode)


def cmd_regenerate(args):
    """Regenerate docker-compose.yml and .swarm/ config files from current config."""
    project_dir = find_project_dir()
    config = load_config(project_dir)
    swarm_dir = os.path.join(project_dir, ".swarm")

    # Regenerate docker-compose.yml
    compose_path = os.path.join(project_dir, "docker-compose.yml")
    compose_content = generate_docker_compose(config)
    with open(compose_path, "w") as f:
        f.write(compose_content)
    print(f"Regenerated docker-compose.yml")

    # Re-copy agent/ files
    agent_src = os.path.join(PROJECT_ROOT_OF_SWARM, "agent")
    if os.path.isdir(agent_src):
        for fname in ("agent-loop.sh", "entrypoint.sh", "Dockerfile"):
            src = os.path.join(agent_src, fname)
            dst = os.path.join(swarm_dir, "agent", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
        print("Updated agent/ files")

    # Re-copy ticket/ files
    ticket_src = os.path.join(PROJECT_ROOT_OF_SWARM, "ticket")
    if os.path.isdir(ticket_src):
        for fname in os.listdir(ticket_src):
            src = os.path.join(ticket_src, fname)
            dst = os.path.join(swarm_dir, "ticket", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
        print("Updated ticket/ files")

    # Re-copy monitor/ files
    monitor_src = os.path.join(PROJECT_ROOT_OF_SWARM, "monitor")
    monitor_dst = os.path.join(swarm_dir, "monitor")
    if os.path.isdir(monitor_src):
        if os.path.isdir(monitor_dst):
            shutil.rmtree(monitor_dst)
        shutil.copytree(monitor_src, monitor_dst)
        print("Updated monitor/ files")

    print("\nDone. Run 'swarm stop && swarm start' to apply changes.")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="swarm",
        description="Bootstrap and manage autonomous agent swarms for any project.",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p = sub.add_parser("init", help="Initialize a project for agent swarms")
    p.add_argument("project_dir", help="Path to the project directory")

    # start
    sub.add_parser("start", help="Spin up agent containers and monitor")

    # stop
    sub.add_parser("stop", help="Shut down all agents and monitor")

    # status
    sub.add_parser("status", help="Show running agents and queue summary")

    # logs
    p = sub.add_parser("logs", help="Tail logs for a specific agent")
    p.add_argument("service", help="Service name (e.g. agent-1)")

    # scale
    p = sub.add_parser("scale", help="Adjust number of agent containers")
    p.add_argument("count", type=int, help="New number of agents")

    # regenerate
    sub.add_parser("regenerate", help="Regenerate docker-compose.yml and .swarm/ files from source")

    return parser


DISPATCH = {
    "init": cmd_init,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "logs": cmd_logs,
    "scale": cmd_scale,
    "regenerate": cmd_regenerate,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(2)

    handler = DISPATCH.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(2)

    handler(args)


if __name__ == "__main__":
    main()
