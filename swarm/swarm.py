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
import sqlite3
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

# Template directory (contains CLAUDE.md, docker-compose.yml, agent-service.yml)
TEMPLATES_DIR = os.path.join(PROJECT_ROOT_OF_SWARM, "templates")

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
Your goal is to populate PROJECT.md and create a verify.sh script.

Ask about these topics (one or two at a time, conversationally):
1. What does this product do?
2. Who are the target users?
3. How will it make money, or what problem does it solve?
4. Are there hard constraints (regulatory, platform, timeline)?
5. What does success look like?

After those questions, propose a tech stack:
6. Based on what you've learned, propose a language and framework (e.g. "Python with FastAPI", \
"TypeScript with Next.js"). Give brief reasoning (1-2 sentences). Ask the human to confirm or suggest changes.

Guidelines:
- Be conversational and brief. Don't overwhelm with questions.
- It's fine to combine or skip questions based on what the human volunteers.
- When you have enough context, write PROJECT.md at the project root using the Write tool.
- Keep PROJECT.md concise — a few clear paragraphs, not an essay.
- Use the same five section headings that already exist in the placeholder PROJECT.md.
- Do NOT make architectural decisions beyond the core language/framework for verify.sh.

After writing PROJECT.md, write a verify.sh script at the project root. This script:
- Runs linting and/or tests appropriate for the confirmed tech stack
- Exits 0 if the project isn't set up yet (e.g. no package.json, no requirements.txt)
- Installs lint/test tools if they're missing (e.g. pip install, npm install)
- Exits 0 on success, non-zero on failure
- Starts with #!/bin/bash and set -euo pipefail

Example verify.sh for a Python project:
```bash
#!/bin/bash
set -euo pipefail
# Exit successfully if project isn't set up yet
[ -f requirements.txt ] || exit 0
pip install -q ruff pytest 2>/dev/null || true
ruff check .
python -m pytest --tb=short -q 2>/dev/null || true
```

Do NOT run chmod on verify.sh — the swarm CLI handles that automatically after the interview.

## Platform considerations

Agents run inside Linux Docker containers. Most tech stacks work natively in Linux (Python, \
Node, Go, Rust, Java, C/C++, .NET, etc.). Some require extra setup:

**Linux-compatible but needs extra packages** (e.g. Android SDK, specific compilers):
After confirming the tech stack, write a project-setup.sh script at .swarm/agent/project-setup.sh \
that installs the required packages. This script runs during Docker image build (as root). \
Keep it minimal — only install what's needed for building and testing.

Example project-setup.sh for an Android project:
```bash
#!/bin/bash
set -euo pipefail
apt-get update && apt-get install -y --no-install-recommends openjdk-17-jdk-headless unzip wget
ANDROID_SDK_ROOT=/opt/android-sdk
mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"
wget -q https://dl.google.com/android/repository/commandlinetools-linux-latest.zip -O /tmp/sdk.zip
unzip -q /tmp/sdk.zip -d "$ANDROID_SDK_ROOT/cmdline-tools"
mv "$ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools" "$ANDROID_SDK_ROOT/cmdline-tools/latest"
yes | "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" --licenses > /dev/null 2>&1
"$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" "platform-tools" "platforms;android-34" "build-tools;34.0.0"
rm /tmp/sdk.zip
```

**Cannot build in Linux** (e.g. iOS/macOS apps requiring Xcode, Windows-native apps):
In this case, verify.sh should:
1. Run whatever checks CAN work in Linux (linting, dependency resolution, syntax checks)
2. Create a ticket assigned to human for manual build/test, then exit 0

The environment variables TICKET_ID, TICKET_TITLE, and AGENT_ID are available inside verify.sh.

Example verify.sh for an iOS project:
```bash
#!/bin/bash
set -euo pipefail

# Skip if project isn't set up yet
[ -f Package.swift ] || [ -d *.xcodeproj ] 2>/dev/null || exit 0

# Checks that work in Linux
if command -v swiftlint &>/dev/null; then
    swiftlint lint --quiet 2>&1 || true
fi

# Platform-specific checks — delegate to human
if [ -n "${TICKET_ID:-}" ]; then
    python3 /usr/local/lib/ticket.py create \
        "Manual build/test needed for ticket-${TICKET_ID}" \
        --description "Ticket-${TICKET_ID} (${TICKET_TITLE:-untitled}) needs manual verification on macOS/iOS. Please: 1) Run 'swarm pull' to get latest code 2) Build the project in Xcode 3) Run tests on simulator 4) If issues found, click Fail with details" \
        --assign human \
        --type verify \
        --block-dependents-of "${TICKET_ID}" \
        --created-by "${AGENT_ID:-verify.sh}"
fi
```

Do NOT write project-setup.sh if the tech stack works natively in Linux without extra packages.

After writing both PROJECT.md and verify.sh, call the mcp__interview__end_interview tool to \
automatically end the session. Do NOT use the Skill tool — use the MCP tool directly.
"""

DEFAULT_CONFIG = {
    "agents": 3,
    "ntfy_topic": "",
    "allowed_tools": "Bash,Read,Write,Edit,Glob,Grep",
    "max_turns": 50,
    "monitor_port": 3000,
    "verify_retries": 2,
}

SEED_TICKET_TITLE = (
    "Decompose project into work tickets"
)

SEED_TICKET_DESCRIPTION = (
    "Read PROJECT.md to understand the product vision and CLAUDE.md for operating guidelines. "
    "Then break the project into concrete, actionable tickets using the ticket CLI.\n\n"
    "IMPORTANT — establish dependency order using --blocked-by:\n"
    "- Create foundational tickets first, then dependent tickets that reference them\n"
    "- Example: 'Setup project structure' (ticket #2) must finish before 'Implement user auth' (ticket #3):\n"
    "    ticket create 'Setup project structure' --parent 1 --created-by $AGENT_ID\n"
    "    ticket create 'Implement user auth' --parent 1 --blocked-by 2 --created-by $AGENT_ID\n\n"
    "Each ticket should be small enough for one agent to complete in a single session. "
    "Include clear descriptions so any agent can pick up the work."
)


# ---------------------------------------------------------------------------
# docker-compose.yml generator
# ---------------------------------------------------------------------------

def generate_docker_compose(config):
    """Return a docker-compose.yml string based on the given config dict.

    Reads templates from TEMPLATES_DIR and substitutes config values.
    The generated file is placed at the PROJECT root and all paths are
    relative to the project root (i.e. the directory that contains .swarm/).
    """
    agent_count = config.get("agents", 3)
    monitor_port = config.get("monitor_port", 3000)
    ntfy_topic = config.get("ntfy_topic", "")
    max_turns = config.get("max_turns", 50)
    allowed_tools = config.get("allowed_tools", "Bash,Read,Write,Edit,Glob,Grep")
    verify_retries = config.get("verify_retries", 2)

    # Read templates
    compose_template_path = os.path.join(TEMPLATES_DIR, "docker-compose.yml")
    agent_template_path = os.path.join(TEMPLATES_DIR, "agent-service.yml")

    with open(compose_template_path, "r") as f:
        compose_template = f.read()
    with open(agent_template_path, "r") as f:
        agent_template = f.read()

    # Generate agent services
    agent_services = []
    for i in range(1, agent_count + 1):
        service = agent_template.replace("__N__", str(i))
        service = service.replace("__MAX_TURNS__", str(max_turns))
        service = service.replace("__ALLOWED_TOOLS__", allowed_tools)
        service = service.replace("__VERIFY_RETRIES__", str(verify_retries))
        # Conditionally include NTFY_TOPIC line
        if ntfy_topic:
            service = service.replace("__NTFY_TOPIC_LINE__", f"      - NTFY_TOPIC={ntfy_topic}\n")
        else:
            service = service.replace("__NTFY_TOPIC_LINE__", "")
        agent_services.append(service)

    # Combine into final compose file
    agent_services_block = "\n".join(agent_services)
    result = compose_template.replace("# __AGENT_SERVICES__", agent_services_block)
    result = result.replace("__MONITOR_PORT__", str(monitor_port))

    return result


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
    try:
        d = os.getcwd()
    except (FileNotFoundError, OSError):
        print("Error: Current directory does not exist. Are you in a swarm project directory?",
              file=sys.stderr)
        sys.exit(1)
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
    os.makedirs(os.path.join(swarm_dir, "agent-logs"), exist_ok=True)  # per-agent session logs
    # ── 2. Copy agent/ files ────────────────────────────────────────────
    agent_src = os.path.join(PROJECT_ROOT_OF_SWARM, "agent")
    if os.path.isdir(agent_src):
        for fname in ("agent-loop.sh", "entrypoint.sh", "Dockerfile", "check-alive.sh"):
            src = os.path.join(agent_src, fname)
            dst = os.path.join(swarm_dir, "agent", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                print(f"  Copied agent/{fname}")
    else:
        print(f"  Warning: {agent_src} not found — skipping agent file copy.")

    # Create default project-setup.sh (no-op; interview may populate)
    setup_sh = os.path.join(swarm_dir, "agent", "project-setup.sh")
    if not os.path.isfile(setup_sh):
        with open(setup_sh, "w") as f:
            f.write("#!/bin/bash\n# Project-specific setup — generated by swarm interview.\n# This runs during Docker image build. Empty by default.\nexit 0\n")
        os.chmod(setup_sh, 0o755)
        print("  Created agent/project-setup.sh (no-op default)")

    # ── 3. Copy ticket/ files (including migrations/) ───────────────────
    ticket_src = os.path.join(PROJECT_ROOT_OF_SWARM, "ticket")
    if os.path.isdir(ticket_src):
        for fname in os.listdir(ticket_src):
            src = os.path.join(ticket_src, fname)
            dst = os.path.join(swarm_dir, "ticket", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                print(f"  Copied ticket/{fname}")
            elif os.path.isdir(src) and fname == "migrations":
                # Copy migrations directory
                migrations_dst = os.path.join(swarm_dir, "ticket", "migrations")
                if os.path.isdir(migrations_dst):
                    shutil.rmtree(migrations_dst)
                shutil.copytree(src, migrations_dst)
                print(f"  Copied ticket/migrations/")
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

    # ── 6. Initialize SQLite database via migrations ────────────────────
    tickets_db_path = os.path.join(swarm_dir, "tickets", "tickets.db")
    ticket_py = os.path.join(swarm_dir, "ticket", "ticket.py")
    if os.path.isfile(ticket_py):
        result = subprocess.run(
            [sys.executable, ticket_py, "--db", tickets_db_path, "migrate"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("  Initialized tickets.db")
        else:
            print(f"  Warning: Migration failed: {result.stderr.strip()}")
    else:
        print("  Warning: ticket.py not found — database not initialized.")

    # ── 7. Update .gitignore (before git init so .swarm/ is never tracked)
    gitignore_path = os.path.join(project_dir, ".gitignore")
    entries_to_add = [".swarm/"]
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

    # ── 9. Copy CLAUDE.md from template to project root ─────────────────
    claude_md_path = os.path.join(project_dir, "CLAUDE.md")
    claude_md_template = os.path.join(TEMPLATES_DIR, "CLAUDE.md")
    if not os.path.isfile(claude_md_path):
        shutil.copy2(claude_md_template, claude_md_path)
        print("  Copied CLAUDE.md")
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

    # Add bare repo as 'swarm' remote for easy pulling
    subprocess.run(
        ["git", "-C", project_dir, "remote", "add", "swarm", ".swarm/repo.git"],
        capture_output=True, text=True,
    )

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

        # Make verify.sh executable (Write tool doesn't set permissions)
        verify_sh_path = os.path.join(project_dir, "verify.sh")
        if os.path.isfile(verify_sh_path):
            os.chmod(verify_sh_path, 0o755)

        # Commit updated PROJECT.md and verify.sh to the bare repo so agents see them
        subprocess.run(["git", "-C", project_dir, "add", "PROJECT.md", "verify.sh"], capture_output=True)
        commit_result = subprocess.run(
            ["git", "-C", project_dir, "commit", "-m", "Add PROJECT.md and verify.sh from interview (swarm init)"],
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
            print("  Updated bare repo with PROJECT.md and verify.sh from interview")

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


def release_agent_tickets(project_dir):
    """Release all non-done tickets assigned to agents (non-human).

    Called on swarm start. At startup no agent is running yet, so any ticket
    still assigned to an agent is orphaned — regardless of status. This covers
    in_progress (normal crash), open (proposal path crash), and ready
    (push-finalization crash).

    Human-assigned tickets are left untouched.

    Returns the number of tickets that were released.
    """
    tickets_db = os.path.join(project_dir, ".swarm", "tickets", "tickets.db")
    if not os.path.isfile(tickets_db):
        return 0

    conn = sqlite3.connect(tickets_db, timeout=10)

    # Find non-done tickets assigned to agents (not human)
    orphaned = conn.execute(
        "SELECT id, assigned_to, status FROM tickets "
        "WHERE assigned_to IS NOT NULL "
        "AND assigned_to != 'human' "
        "AND status != 'done'"
    ).fetchall()

    if not orphaned:
        conn.close()
        return 0

    # Log activity and release each ticket
    for ticket_id, agent_id, status in orphaned:
        conn.execute(
            "INSERT INTO activity_log (ticket_id, agent_id, action, detail) "
            "VALUES (?, ?, 'unclaimed', 'Auto-released on swarm start')",
            (ticket_id, agent_id)
        )
        conn.execute(
            "UPDATE tickets SET assigned_to = NULL, status = 'open', "
            "updated_at = datetime('now') WHERE id = ?",
            (ticket_id,)
        )

    conn.commit()
    conn.close()
    return len(orphaned)


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

    # Release any tickets still assigned to agents from previous run
    count = release_agent_tickets(project_dir)
    if count > 0:
        print(f"Released {count} orphaned agent ticket(s) from previous run.")

    # Run database migrations before starting
    ticket_py = os.path.join(project_dir, ".swarm", "ticket", "ticket.py")
    tickets_db = os.path.join(project_dir, ".swarm", "tickets", "tickets.db")
    if os.path.isfile(ticket_py):
        print("Running database migrations...")
        result = subprocess.run(
            [sys.executable, ticket_py, "--db", tickets_db, "migrate"],
            cwd=project_dir,
        )
        if result.returncode != 0:
            print("Error: Database migration failed.", file=sys.stderr)
            sys.exit(1)

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

    # Ensure agent-logs directory exists
    os.makedirs(os.path.join(swarm_dir, "agent-logs"), exist_ok=True)

    # Re-copy agent/ files
    agent_src = os.path.join(PROJECT_ROOT_OF_SWARM, "agent")
    if os.path.isdir(agent_src):
        for fname in ("agent-loop.sh", "entrypoint.sh", "Dockerfile", "check-alive.sh"):
            src = os.path.join(agent_src, fname)
            dst = os.path.join(swarm_dir, "agent", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
        print("Updated agent/ files")

    # Preserve project-setup.sh (interview-generated, not in source repo)
    setup_sh = os.path.join(swarm_dir, "agent", "project-setup.sh")
    if not os.path.isfile(setup_sh):
        with open(setup_sh, "w") as f:
            f.write("#!/bin/bash\n# Project-specific setup — generated by swarm interview.\n# This runs during Docker image build. Empty by default.\nexit 0\n")
        os.chmod(setup_sh, 0o755)

    # Re-copy ticket/ files (including migrations/)
    ticket_src = os.path.join(PROJECT_ROOT_OF_SWARM, "ticket")
    if os.path.isdir(ticket_src):
        for fname in os.listdir(ticket_src):
            src = os.path.join(ticket_src, fname)
            dst = os.path.join(swarm_dir, "ticket", fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
            elif os.path.isdir(src) and fname == "migrations":
                migrations_dst = os.path.join(swarm_dir, "ticket", "migrations")
                if os.path.isdir(migrations_dst):
                    shutil.rmtree(migrations_dst)
                shutil.copytree(src, migrations_dst)
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


def cmd_pull(args):
    """Pull latest changes from the swarm bare repo."""
    project_dir = find_project_dir()
    # Ensure remote exists
    subprocess.run(
        ["git", "-C", project_dir, "remote", "add", "swarm", ".swarm/repo.git"],
        capture_output=True, text=True,
    )
    result = subprocess.run(
        ["git", "-C", project_dir, "pull", "swarm", "main", "--ff-only"],
    )
    if result.returncode != 0:
        print("\nFast-forward pull failed. You can try:", file=sys.stderr)
        print("  git merge swarm/main", file=sys.stderr)
        print("  git rebase swarm/main", file=sys.stderr)
        sys.exit(result.returncode)


def cmd_watch(args):
    """Watch for new commits in the swarm bare repo and auto-pull."""
    project_dir = find_project_dir()
    bare_repo = os.path.join(project_dir, ".swarm", "repo.git")
    interval = args.interval

    # Ensure remote exists
    subprocess.run(
        ["git", "-C", project_dir, "remote", "add", "swarm", ".swarm/repo.git"],
        capture_output=True, text=True,
    )

    def get_head():
        r = subprocess.run(
            ["git", "-C", bare_repo, "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else None

    last_hash = get_head()
    print(f"Watching {bare_repo} every {interval}s (Ctrl+C to stop)")
    print(f"Current HEAD: {last_hash or '(unknown)'}")

    try:
        while True:
            time.sleep(interval)
            current = get_head()
            if current and current != last_hash:
                print(f"\nNew commit detected: {current[:12]}")
                result = subprocess.run(
                    ["git", "-C", project_dir, "pull", "swarm", "main", "--ff-only"],
                )
                if result.returncode == 0:
                    print("Pulled successfully.")
                else:
                    print("Fast-forward failed — manual merge needed.", file=sys.stderr)
                last_hash = current
    except KeyboardInterrupt:
        print("\nStopped watching.")


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

    # pull
    sub.add_parser("pull", help="Pull latest changes from the swarm bare repo")

    # watch
    p = sub.add_parser("watch", help="Watch for new commits and auto-pull")
    p.add_argument("--interval", type=int, default=5, help="Poll interval in seconds (default: 5)")

    return parser


DISPATCH = {
    "init": cmd_init,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "logs": cmd_logs,
    "scale": cmd_scale,
    "regenerate": cmd_regenerate,
    "pull": cmd_pull,
    "watch": cmd_watch,
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
