#!/usr/bin/env python3
"""Test suite for the ticket CLI.

Exercises every command via subprocess and verifies outputs and exit codes.
Uses a temporary database for each test run.
"""

import json
import os
import subprocess
import sys
import tempfile

TICKET_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket.py")
PYTHON = sys.executable

passed = 0
failed = 0


def run(args, expect_rc=0, db=None):
    """Run the ticket CLI with the given arguments."""
    cmd = [PYTHON, TICKET_PY]
    if db:
        cmd.extend(["--db", db])
    cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def assert_eq(label, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {label}")
        print(f"    expected: {expected!r}")
        print(f"    actual:   {actual!r}")


def assert_in(label, haystack, needle):
    global passed, failed
    if needle in haystack:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {label}")
        print(f"    expected to find: {needle!r}")
        print(f"    in: {haystack!r}")


def assert_rc(label, result, expected_rc):
    global passed, failed
    if result.returncode == expected_rc:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {label} (exit code)")
        print(f"    expected rc: {expected_rc}")
        print(f"    actual rc:   {result.returncode}")
        print(f"    stdout: {result.stdout[:500]}")
        print(f"    stderr: {result.stderr[:500]}")


def test_create_and_show():
    print("test_create_and_show")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        # Create a ticket
        r = run(["create", "First ticket", "--description", "Some details",
                 "--created-by", "agent-1"], db=db)
        assert_rc("create rc", r, 0)
        ticket_id = r.stdout.strip()
        assert_eq("create returns id", ticket_id, "1")

        # Show it in text format
        r = run(["show", "1"], db=db)
        assert_rc("show rc", r, 0)
        assert_in("show title", r.stdout, "First ticket")
        assert_in("show status", r.stdout, "open")
        assert_in("show created_by", r.stdout, "agent-1")
        assert_in("show description", r.stdout, "Some details")

        # Show it in JSON format
        r = run(["show", "1", "--format", "json"], db=db)
        assert_rc("show json rc", r, 0)
        data = json.loads(r.stdout)
        assert_eq("json title", data["title"], "First ticket")
        assert_eq("json status", data["status"], "open")
        assert_eq("json created_by", data["created_by"], "agent-1")

        # Show nonexistent ticket
        r = run(["show", "999"], db=db)
        assert_rc("show missing rc", r, 1)
    finally:
        os.unlink(db)


def test_update():
    print("test_update")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Original title"], db=db)

        r = run(["update", "1", "--title", "Updated title", "--status", "in_progress",
                 "--assign", "agent-2"], db=db)
        assert_rc("update rc", r, 0)

        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("updated title", data["title"], "Updated title")
        assert_eq("updated status", data["status"], "in_progress")
        assert_eq("updated assigned", data["assigned_to"], "agent-2")

        # Update nonexistent
        r = run(["update", "999", "--title", "nope"], db=db)
        assert_rc("update missing rc", r, 1)

        # Update nothing
        r = run(["update", "1"], db=db)
        assert_rc("update nothing rc", r, 2)
    finally:
        os.unlink(db)


def test_list():
    print("test_list")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Task A"], db=db)
        run(["create", "Task B", "--assign", "agent-1"], db=db)
        run(["create", "Task C"], db=db)
        # Mark one as done
        run(["complete", "3"], db=db)

        # Default list (non-done)
        r = run(["list"], db=db)
        assert_rc("list rc", r, 0)
        assert_in("list has A", r.stdout, "Task A")
        assert_in("list has B", r.stdout, "Task B")
        # Task C is done, should not appear
        lines = [l for l in r.stdout.splitlines() if "Task C" in l]
        assert_eq("list excludes done", len(lines), 0)

        # Filter by status
        r = run(["list", "--status", "done"], db=db)
        assert_in("list done has C", r.stdout, "Task C")

        # Filter by assigned_to
        r = run(["list", "--assigned-to", "agent-1"], db=db)
        assert_in("list assigned has B", r.stdout, "Task B")
        lines = [l for l in r.stdout.splitlines() if "Task A" in l]
        assert_eq("list assigned excludes A", len(lines), 0)

        # JSON format
        r = run(["list", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("list json count", len(data), 2)

    finally:
        os.unlink(db)


def test_count():
    print("test_count")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "A"], db=db)
        run(["create", "B"], db=db)
        run(["create", "C"], db=db)
        run(["complete", "3"], db=db)

        r = run(["count"], db=db)
        assert_eq("count non-done", r.stdout.strip(), "2")

        r = run(["count", "--status", "open"], db=db)
        assert_eq("count open", r.stdout.strip(), "2")

        r = run(["count", "--status", "done"], db=db)
        assert_eq("count done", r.stdout.strip(), "1")

        r = run(["count", "--status", "open,done"], db=db)
        assert_eq("count open+done", r.stdout.strip(), "3")
    finally:
        os.unlink(db)


def test_claim_next():
    print("test_claim_next")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Task 1"], db=db)
        run(["create", "Task 2"], db=db)
        run(["create", "Task 3"], db=db)

        # Claim first
        r = run(["claim-next", "--agent", "agent-A"], db=db)
        assert_rc("claim rc", r, 0)
        assert_in("claim shows task 1", r.stdout, "Task 1")
        assert_in("claim shows agent", r.stdout, "agent-A")

        # Verify it's in_progress
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("claimed status", data["status"], "in_progress")
        assert_eq("claimed assigned", data["assigned_to"], "agent-A")

        # Claim next should get task 2
        r = run(["claim-next", "--agent", "agent-B", "--format", "json"], db=db)
        assert_rc("claim 2 rc", r, 0)
        data = json.loads(r.stdout)
        assert_eq("claim 2 id", data["id"], 2)

        # Claim next should get task 3
        r = run(["claim-next", "--agent", "agent-C"], db=db)
        assert_rc("claim 3 rc", r, 0)
        assert_in("claim 3 title", r.stdout, "Task 3")

        # No more tickets to claim
        r = run(["claim-next", "--agent", "agent-D"], db=db)
        assert_rc("claim empty rc", r, 1)
    finally:
        os.unlink(db)


def test_claim_next_with_blockers():
    print("test_claim_next_with_blockers")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        # Create ticket 1 and ticket 2; ticket 2 is blocked by ticket 1
        run(["create", "Prerequisite"], db=db)
        run(["create", "Depends on prereq"], db=db)
        run(["block", "2", "--by", "1"], db=db)

        # Claim next should get ticket 1 (ticket 2 is blocked)
        r = run(["claim-next", "--agent", "agent-A", "--format", "json"], db=db)
        assert_rc("claim blocked rc", r, 0)
        data = json.loads(r.stdout)
        assert_eq("claim blocked picks 1", data["id"], 1)

        # No more claimable (ticket 2 is blocked, ticket 1 is claimed)
        r = run(["claim-next", "--agent", "agent-B"], db=db)
        assert_rc("claim blocked nothing rc", r, 1)

        # Complete ticket 1 (the blocker)
        run(["complete", "1"], db=db)

        # Now ticket 2 should be claimable
        r = run(["claim-next", "--agent", "agent-B", "--format", "json"], db=db)
        assert_rc("claim unblocked rc", r, 0)
        data = json.loads(r.stdout)
        assert_eq("claim unblocked picks 2", data["id"], 2)
    finally:
        os.unlink(db)


def test_comment_and_comments():
    print("test_comment_and_comments")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Commentable"], db=db)

        r = run(["comment", "1", "First comment", "--author", "agent-1"], db=db)
        assert_rc("comment rc", r, 0)
        assert_in("comment confirm", r.stdout, "Comment added")

        r = run(["comment", "1", "Second comment"], db=db)
        assert_rc("comment 2 rc", r, 0)

        # List comments text
        r = run(["comments", "1"], db=db)
        assert_rc("comments rc", r, 0)
        assert_in("comments has first", r.stdout, "First comment")
        assert_in("comments has second", r.stdout, "Second comment")
        assert_in("comments has author", r.stdout, "agent-1")

        # List comments JSON
        r = run(["comments", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("comments json count", len(data), 2)
        assert_eq("comments json body", data[0]["body"], "First comment")

        # Comment on nonexistent
        r = run(["comment", "999", "nope"], db=db)
        assert_rc("comment missing rc", r, 1)

        # Comments on nonexistent
        r = run(["comments", "999"], db=db)
        assert_rc("comments missing rc", r, 1)
    finally:
        os.unlink(db)


def test_complete_and_unclaim():
    print("test_complete_and_unclaim")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Work item"], db=db)
        run(["claim-next", "--agent", "agent-1"], db=db)

        # Unclaim
        r = run(["unclaim", "1"], db=db)
        assert_rc("unclaim rc", r, 0)
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("unclaimed status", data["status"], "open")
        assert_eq("unclaimed assigned", data["assigned_to"], None)

        # Re-claim and complete
        run(["claim-next", "--agent", "agent-2"], db=db)
        r = run(["complete", "1"], db=db)
        assert_rc("complete rc", r, 0)
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("completed status", data["status"], "done")

        # Complete nonexistent
        r = run(["complete", "999"], db=db)
        assert_rc("complete missing rc", r, 1)
    finally:
        os.unlink(db)


def test_block_and_unblock():
    print("test_block_and_unblock")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Ticket A"], db=db)
        run(["create", "Ticket B"], db=db)

        # Block
        r = run(["block", "1", "--by", "2"], db=db)
        assert_rc("block rc", r, 0)
        assert_in("block msg", r.stdout, "blocked by")

        # Show should include blocker info
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_in("blocked_by in show", data["blocked_by"], 2)

        # Duplicate block
        r = run(["block", "1", "--by", "2"], db=db)
        assert_rc("dup block rc", r, 1)

        # Unblock
        r = run(["unblock", "1", "--by", "2"], db=db)
        assert_rc("unblock rc", r, 0)
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("unblocked", data["blocked_by"], [])

        # Unblock nonexistent relationship
        r = run(["unblock", "1", "--by", "2"], db=db)
        assert_rc("unblock missing rc", r, 1)

        # Block nonexistent ticket
        r = run(["block", "999", "--by", "1"], db=db)
        assert_rc("block missing ticket rc", r, 1)
    finally:
        os.unlink(db)


def test_create_with_blocks():
    print("test_create_with_blocks")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        # Create ticket 1 first
        run(["create", "Main task"], db=db)

        # Create ticket 2 that blocks ticket 1
        r = run(["create", "Subtask", "--blocks", "1", "--created-by", "agent-1"], db=db)
        assert_rc("create blocks rc", r, 0)
        assert_eq("create blocks id", r.stdout.strip(), "2")

        # Ticket 1 should now be blocked by ticket 2
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_in("blocked by subtask", data["blocked_by"], 2)

        # Ticket 2 should show it blocks ticket 1
        r = run(["show", "2", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_in("blocks main", data["blocks"], 1)
    finally:
        os.unlink(db)


def test_parent():
    print("test_parent")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Parent task"], db=db)
        r = run(["create", "Child task", "--parent", "1"], db=db)
        assert_eq("child id", r.stdout.strip(), "2")

        r = run(["show", "2", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("parent_id", data["parent_id"], 1)
    finally:
        os.unlink(db)


def test_activity_log():
    print("test_activity_log")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Logged task", "--created-by", "agent-1"], db=db)
        run(["comment", "1", "A comment", "--author", "agent-1"], db=db)
        run(["claim-next", "--agent", "agent-2"], db=db)
        run(["complete", "1"], db=db)

        r = run(["log", "--limit", "10"], db=db)
        assert_rc("log rc", r, 0)
        assert_in("log has created", r.stdout, "created")
        assert_in("log has commented", r.stdout, "commented")
        assert_in("log has claimed", r.stdout, "claimed")
        assert_in("log has completed", r.stdout, "completed")

        # Log with default limit
        r = run(["log"], db=db)
        assert_rc("log default rc", r, 0)
    finally:
        os.unlink(db)


def test_default_created_by():
    print("test_default_created_by")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        r = run(["create", "Human ticket"], db=db)
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("default created_by", data["created_by"], "human")
    finally:
        os.unlink(db)


def test_no_command():
    print("test_no_command")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        r = run([], db=db)
        assert_rc("no command rc", r, 2)
    finally:
        os.unlink(db)


def test_show_text_blockers():
    print("test_show_text_blockers")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "Task A"], db=db)
        run(["create", "Task B"], db=db)
        run(["block", "1", "--by", "2"], db=db)

        # Text show should display blocker info
        r = run(["show", "1"], db=db)
        assert_in("text blocked by", r.stdout, "Blocked by:")

        # Ticket 2 should show what it blocks
        r = run(["show", "2"], db=db)
        assert_in("text blocks", r.stdout, "Blocks:")
    finally:
        os.unlink(db)


def test_list_empty():
    print("test_list_empty")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        r = run(["list"], db=db)
        assert_rc("list empty rc", r, 0)
        assert_in("list empty msg", r.stdout, "No tickets found")

        r = run(["list", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("list empty json", data, [])
    finally:
        os.unlink(db)


def test_comments_empty():
    print("test_comments_empty")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "No comments"], db=db)
        r = run(["comments", "1"], db=db)
        assert_rc("comments empty rc", r, 0)
        assert_in("comments empty msg", r.stdout, "No comments")

        r = run(["comments", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("comments empty json", data, [])
    finally:
        os.unlink(db)


def test_show_with_comments():
    print("test_show_with_comments")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        run(["create", "With comments"], db=db)
        run(["comment", "1", "Hello world", "--author", "tester"], db=db)

        # Text show should include comments section
        r = run(["show", "1"], db=db)
        assert_in("show comments section", r.stdout, "Comments (1)")
        assert_in("show comment body", r.stdout, "Hello world")
        assert_in("show comment author", r.stdout, "tester")

        # JSON show should include comments array
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("show json comments count", len(data["comments"]), 1)
        assert_eq("show json comment body", data["comments"][0]["body"], "Hello world")
    finally:
        os.unlink(db)


def test_block_auto_unclaims():
    print("test_block_auto_unclaims")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    try:
        # Create two tickets
        run(["create", "Main work"], db=db)
        run(["create", "Prerequisite"], db=db)

        # Claim ticket 1
        run(["claim-next", "--agent", "agent-1"], db=db)
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("pre-block status", data["status"], "in_progress")
        assert_eq("pre-block assigned", data["assigned_to"], "agent-1")

        # Block ticket 1 by ticket 2 — should auto-unclaim
        r = run(["block", "1", "--by", "2"], db=db)
        assert_rc("block rc", r, 0)

        # Ticket 1 should now be open and unassigned
        r = run(["show", "1", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("post-block status", data["status"], "open")
        assert_eq("post-block assigned", data["assigned_to"], None)
        assert_in("post-block blocker", data["blocked_by"], 2)

        # Blocking an unclaimed ticket should still work (no-op on unclaim)
        run(["create", "Another task"], db=db)
        r = run(["block", "3", "--by", "2"], db=db)
        assert_rc("block unclaimed rc", r, 0)
        r = run(["show", "3", "--format", "json"], db=db)
        data = json.loads(r.stdout)
        assert_eq("unclaimed stays open", data["status"], "open")
        assert_eq("unclaimed stays null", data["assigned_to"], None)
    finally:
        os.unlink(db)


if __name__ == "__main__":
    tests = [
        test_create_and_show,
        test_update,
        test_list,
        test_count,
        test_claim_next,
        test_claim_next_with_blockers,
        test_comment_and_comments,
        test_complete_and_unclaim,
        test_block_and_unblock,
        test_create_with_blocks,
        test_parent,
        test_activity_log,
        test_default_created_by,
        test_no_command,
        test_show_text_blockers,
        test_list_empty,
        test_comments_empty,
        test_show_with_comments,
        test_block_auto_unclaims,
    ]

    for test in tests:
        test()

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*50}")

    if failed > 0:
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)
