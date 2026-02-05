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

## Decision Making

- **Technical decisions** (database, framework, architecture): Make the call, document in a comment
- **Business decisions** (users, monetization, direction): Create a human-assigned blocking ticket
