# Agent Operating Manual

This file guides autonomous AI agents working on this project.
Read PROJECT.md for business context and product vision.

## Workflow

### Sub-Agents

Use sub-agents **liberally**. Context rot is a real problem — iteration, failed experiments, dead-ends, errors, and other token bloat accumulate and degrade output quality. The ideal workflow is for the top-level agent to be an **orchestrator** that delegates to sub-agents:

1. **Research** (sub-agent) — outputs a problem statement
2. **Explore** (sub-agent) — reads code, outputs relevant file/function pointers
3. **Plan** (sub-agent) — outputs an implementation plan
4. **Implement** (sub-agent) — executes the plan

Begin every sub-agent prompt with `"YOU ARE THE SUB-AGENT"` so it does not attempt to recursively spawn its own sub-agents.

### Artifacts

Any intermediate artifacts (problem statements, research notes, plans) should be:

- Written to `/tmp` (or another temporary location)
- Cleaned up when the workflow is complete

Do **not** leave `IMPLEMENTATION_PLAN.md`, `RESEARCH_NOTES.md`, or similar files in the codebase. Do not document long-term goals or desired improvements in the codebase — that's what the ticketing system is for.

The codebase holds exactly two things:

1. **Code** — the current state of the system
2. **READMEs** — business context relevant to that code

## Code Organization

### Contain Complexity

Complexity is not the enemy — **uncontained** complexity is. When something is inherently complex, isolate it in its own module behind a simple interface.

*Example: Video transcoding is complex, but `transcode(file, "mp4")` is simple. The complexity exists, but it's contained — callers don't need to understand it.*

### Directory Structure

Use directories for both code organization and **progressive disclosure**. Deeply nested structures allow an agent to read only the READMEs and files relevant to its current task, rather than loading the entire codebase. This enforces separation of concerns and enables documentation at multiple abstraction layers.

Prefer this over a single flat `src/` folder.

### Separation of Concerns

Each file should contain **one level of abstraction** and be responsible for **one thing**. The golden rule (aspirational, not always achievable): any change to behavior should require editing only **one file**. If a change touches many files, the logic wasn't properly encapsulated.

### Reusability

Favor simple, reusable components over complex, monolithic ones. If you need the same code in two places, abstract it into a shared component. Avoid copy-paste duplication.

### Third-Party Dependencies

Third-party libraries are fine, but vendor lock-in is not. Wrap external dependencies in an interface layer so the implementation can be swapped later without rewriting callers.

## Design Guidance

### Patterns

- **Context pattern** — avoid global variables by passing a context object
- **Strategy pattern** — avoid switch statements and complex conditionals by delegating to interchangeable strategy objects
- **Validator / policy iteration** — when logic requires many conditional checks, express them as a list of validator or policy objects and iterate over them

### Correctness

Favor **always-right** solutions over **usually-right** solutions.

- Parse structured data with a proper parser, not a regex that handles 90% of cases
- Use enums or typed state objects instead of stringly-typed status values
- Choose the approach that is correct by construction, not correct by convention

## Documentation

### README Files

Write README files to document **business decisions and intent** — the "why" that isn't obvious from code.

*Example: A video transcoding module's code tells you what it does. But it doesn't tell you why it exists. A README could explain: "The third-party service we integrate with only supports MP4, but users frequently upload MOV files from iPhones."*

README rules:

- **Scope them to modules.** One global README documenting every business decision pulls in irrelevant context. A README in each module directory means agents only load what's relevant to their task.
- **Keep them small.** A README should capture the rationale for the module's existence and any non-obvious constraints. It is not a tutorial or API reference.
- **Update them when code changes.** A stale README is worse than no README. When you change a module's behavior, update its README to match.

### Why READMEs Matter

All code was written with some goal in mind. If you don't know **why** code exists, it becomes dangerous to modify or remove — maybe it handles an edge case or business requirement you don't understand. READMEs preserve that intent so future agents can ensure compliance with the needs the code was written to address.

## Decision Making

- **Technical decisions** (database, framework, architecture): Make the call, document in a comment
- **Business decisions** (users, monetization, direction): Create a human-assigned blocking ticket

## Ticket CLI Reference

All commands use `ticket --db /tickets/tickets.db`.

| Action | Command |
|--------|---------|
| Claim work | `claim-next --agent $AGENT_ID` |
| Log progress | `comment <ID> "message" --author $AGENT_ID` |
| Break down work | `create "Sub-task" --parent <ID> --created-by $AGENT_ID` |
| Depends on other work | `create "Task" --blocked-by <PREREQUISITE_ID> --created-by $AGENT_ID` |
| Mark blocked | `block <ID> --by <BLOCKER_ID>` (auto-releases the ticket) |
| Ask humans | `create "Question" --assign human --blocked-by <YOUR_ID> --created-by $AGENT_ID` |
| Propose improvement | `create "Suggestion" --assign human --type proposal --created-by $AGENT_ID` |
| Release if stuck | `unclaim <ID>` |
| Signal work finished | `complete <ID>` |

## Ticket Types

- **task** (default): Normal work for agents to complete
- **question**: You need human input before continuing. Use `--blocked-by` to block your current ticket.
- **proposal**: Suggesting an improvement. Human will approve/reject. No blocker needed.

When creating a human-assigned ticket:
- With `--blocked-by`: defaults to `question` type
- Without `--blocked-by`: defaults to `proposal` type

## Dependencies

`--blocked-by <ID>` means "this new ticket cannot start until ticket <ID> is done." Create foundational tickets first, then dependent tickets with `--blocked-by`.

If your current ticket depends on unfinished work, run `ticket block <YOUR_ID> --by <PREREQUISITE_ID>` — this automatically releases your ticket back to the pool. Once the prerequisite is done, your ticket becomes claimable again.
