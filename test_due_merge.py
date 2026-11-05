#!/usr/bin/env python3
"""
Tests for the due-date reconciliation rules.

Pure logic only: no Asana or Notion calls are made, so this is safe to run any
time. Needs ASANA_TOKEN and NOTION_TOKEN present (the module checks for them at
import), but never uses them.

    python test_due_merge.py
"""

import sys

import asana_notion_sync as s

BASELINE = "2026-11-10"
ASANA_MOVED = "2026-11-12"
NOTION_MOVED = "2026-11-20"

NO_BASELINE = object()


def state_with(baseline):
    """A sync state holding one task, optionally with a recorded baseline."""
    tasks = {} if baseline is NO_BASELINE else {"T": {"due": baseline}}
    return {"tasks": tasks, "projects": {}}


# (label, baseline, asana_due, notion_due, expected winner)
CASES = [
    ("both sides unchanged",            BASELINE,    BASELINE,     BASELINE,     "agree"),
    ("both empty, no baseline",         NO_BASELINE, None,         None,         "agree"),
    ("Asana moved, Notion didn't",      BASELINE,    ASANA_MOVED,  BASELINE,     "asana"),
    ("Notion moved, Asana didn't",      BASELINE,    BASELINE,     NOTION_MOVED, "notion"),
    ("both moved",                      BASELINE,    ASANA_MOVED,  NOTION_MOVED, "skip"),
    ("Asana cleared its date",          BASELINE,    None,         BASELINE,     "asana"),
    ("Notion cleared its date",         BASELINE,    BASELINE,     None,         "notion"),
    ("Asana cleared, Notion moved",     BASELINE,    None,         NOTION_MOVED, "skip"),
    ("Asana set a first date",          None,        ASANA_MOVED,  None,         "asana"),
    ("Notion set a first date",         None,        None,         NOTION_MOVED, "notion"),
    ("no baseline, Notion empty",       NO_BASELINE, ASANA_MOVED,  None,         "asana"),
    ("no baseline, Asana empty",        NO_BASELINE, None,         NOTION_MOVED, "notion"),
    ("no baseline, both hold a date",   NO_BASELINE, ASANA_MOVED,  NOTION_MOVED, "skip"),
]


def run_table() -> int:
    """Unattended runs must never pick a side on a genuine conflict."""
    s.ALLOW_PROMPT = False
    failures = 0
    for label, baseline, asana_due, notion_due, want in CASES:
        got = s.choose_due_winner(state_with(baseline), "T", "task", "", asana_due, notion_due)
        ok = got == want
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<30} -> {got:<6} (want {want})")
    return failures


def run_prompt_cases() -> int:
    """With a terminal attached, the answer typed decides the winner."""
    s.ALLOW_PROMPT = True
    s.can_prompt = lambda: True
    failures = 0

    for typed, want, sticky_after in [
        ("a", "asana", None),
        ("n", "notion", None),
        ("s", "skip", None),
        ("A", "asana", "asana"),
        ("N", "notion", "notion"),
    ]:
        s._sticky_choice = None
        answers = iter([typed])
        s.__builtins__["input"] = lambda _="": next(answers)
        got = s.choose_due_winner(
            state_with(BASELINE), "T", "task", "", ASANA_MOVED, NOTION_MOVED
        )
        ok = got == want and s._sticky_choice == sticky_after
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  typed {typed!r:<4} -> {got:<6} "
              f"(want {want}, sticky={s._sticky_choice})")

    # A bad answer is re-asked rather than guessed at.
    s._sticky_choice = None
    answers = iter(["x", "", "n"])
    s.__builtins__["input"] = lambda _="": next(answers)
    got = s.choose_due_winner(state_with(BASELINE), "T", "task", "", ASANA_MOVED, NOTION_MOVED)
    ok = got == "notion"
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  invalid answers re-asked -> {got} (want notion)")

    # A sticky answer skips the prompt entirely for the rest of the run.
    s._sticky_choice = "asana"
    s.__builtins__["input"] = lambda _="": sys.exit("prompted despite a sticky answer")
    got = s.choose_due_winner(state_with(BASELINE), "T", "task", "", ASANA_MOVED, NOTION_MOVED)
    ok = got == "asana"
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  sticky answer reused without asking -> {got} (want asana)")
    s._sticky_choice = None

    return failures


if __name__ == "__main__":
    print("Decision table (no terminal):")
    failures = run_table()
    print("\nInteractive prompt:")
    failures += run_prompt_cases()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)
