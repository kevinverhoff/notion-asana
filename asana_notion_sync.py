#!/usr/bin/env python3
"""
Asana <-> Notion bidirectional task sync.

Pulls new/changed Asana tasks into the Notion "All Tasks" database.
Closes Asana tasks that are marked Done in Notion.
Closes Notion projects whose Asana project is completed or archived.
Reconciles due dates in both directions, asking before overwriting a conflict.
Schedule at 8 AM and 5 PM daily.

Usage:
    Create a .env file with:
        ASANA_TOKEN=<personal access token>
        NOTION_TOKEN=<integration token>
    python asana_notion_sync.py              # full sync
    python asana_notion_sync.py --dry-run    # report changes, write nothing
    python asana_notion_sync.py --no-prompt  # never ask; log conflicts instead

Due dates are reconciled against a baseline of what both sides agreed on at the
end of the last run, kept in .sync_state.json. If only one side moved a date,
that change wins automatically. If both moved, it is a conflict: you are asked
which to keep when running in a terminal, and it is logged and left untouched
otherwise.

Dependencies:
    pip install requests python-dotenv
"""

import json
import os
import sys
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("Missing dependency: pip install requests python-dotenv")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Missing dependency: pip install python-dotenv")

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# â”€â”€ Credentials â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
ASANA_TOKEN = os.environ.get("ASANA_TOKEN", "")

if not NOTION_TOKEN or not ASANA_TOKEN:
    sys.exit("Set NOTION_TOKEN and ASANA_TOKEN environment variables before running.")

# â”€â”€ Static IDs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

NOTION_API = "https://api.notion.com/v1"
ASANA_API = "https://app.asana.com/api/1.0"

# Notion database IDs
ALL_TASKS_DB = "203e830a-f6d9-8027-97d6-eefe32d3c77d"
PROJECTS_DB = "203e830a-f6d9-8067-a4f0-e70ee520d0e5"
MEETING_NOTES_DB = "203e830a-f6d9-8097-859b-ddfdace14cfa"  # used for view config

# Page ID extracted from https://app.notion.com/p/353e830af6d98173b94cd0438ed4e012
KTAF_DOMAIN_PAGE_ID = "353e830a-f6d9-8173-b94c-d0438ed4e012"

ASANA_USER_GID = "1159432635245942"

TODAY = date.today().isoformat()

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
ASANA_HEADERS = {
    "Authorization": f"Bearer {ASANA_TOKEN}",
    "Accept": "application/json",
}

# Baseline of what Asana and Notion agreed on at the end of the last run, so a
# one-sided change can be told apart from a genuine two-sided conflict.
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / ".sync_state.json"
CONFLICT_LOG = SCRIPT_DIR / "logs" / "conflicts.json"
STATE_VERSION = 1

# Set from the command line in __main__.
DRY_RUN = False
ALLOW_PROMPT = True

# â”€â”€ TEMPORARY: API usage tracking (remove once the call/data question is answered) â”€â”€

_api_stats: dict[str, dict[str, int]] = {
    "notion": {"calls": 0, "bytes_sent": 0, "bytes_received": 0},
    "asana": {"calls": 0, "bytes_sent": 0, "bytes_received": 0},
}


def _track(service: str, req_body: Optional[dict], resp: requests.Response) -> None:
    stats = _api_stats[service]
    stats["calls"] += 1
    if req_body is not None:
        stats["bytes_sent"] += len(json.dumps(req_body).encode("utf-8"))
    stats["bytes_received"] += len(resp.content)


def did(verb: str) -> str:
    """Phrase an action log line correctly for a real run or a dry run."""
    return f"would {verb.lower()}" if DRY_RUN else verb


def print_api_stats() -> None:
    print("\nAPI usage this run:")
    for service, stats in _api_stats.items():
        total_kb = (stats["bytes_sent"] + stats["bytes_received"]) / 1024
        print(
            f"  {service.capitalize()}: {stats['calls']} calls, "
            f"{stats['bytes_sent']:,} bytes sent / {stats['bytes_received']:,} bytes received "
            f"({total_kb:.1f} KB total)"
        )


# â”€â”€ Sync state (last-agreed baseline) âââââââââââââââââââââââââââââââââââ

def load_state() -> dict:
    """
    Load the last-agreed baseline, or an empty one on a first run.

    A missing or unreadable file is not an error: the run just has no baseline,
    so every difference is treated as a conflict rather than guessed at.
    """
    if not STATE_FILE.exists():
        log.info("No sync state file yet - this run will establish the baseline.")
        return {"version": STATE_VERSION, "tasks": {}, "projects": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s (%s) - starting from an empty baseline.", STATE_FILE.name, exc)
        return {"version": STATE_VERSION, "tasks": {}, "projects": {}}
    state.setdefault("tasks", {})
    state.setdefault("projects", {})
    return state


def save_state(state: dict) -> None:
    """Write the baseline atomically, so an interrupted run can't truncate it."""
    if DRY_RUN:
        log.info("[dry-run] would save sync state (%d task baselines)", len(state.get("tasks", {})))
        return
    state["version"] = STATE_VERSION
    state["last_sync"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log.error("Could not save sync state to %s: %s", STATE_FILE, exc)


def record_due_baseline(state: dict, gid: str, due) -> None:
    """Remember the due date both sides agree on, now that they do."""
    state["tasks"].setdefault(gid, {})["due"] = due


# â”€â”€ Conflict handling âââââââââââââââââââââââââââââââââââââââââââââââââââ

_conflicts: list[dict] = []

# Remembers an "apply to all remaining" answer for the rest of the run.
_sticky_choice: Optional[str] = None


def can_prompt() -> bool:
    """True when there is a human on the other end of stdin to ask."""
    return ALLOW_PROMPT and sys.stdin.isatty() and sys.stdout.isatty()


def _fmt(value: Optional[str]) -> str:
    return value if value else "(none)"


def resolve_due_conflict(
    name: str,
    gid: str,
    link: str,
    asana_due: Optional[str],
    notion_due: Optional[str],
    baseline: Optional[str],
    had_baseline: bool,
) -> str:
    """
    Decide which side's due date wins. Returns "asana", "notion" or "skip".

    Asks when a terminal is attached; otherwise records the conflict and skips,
    so an unattended run never silently picks a side.
    """
    global _sticky_choice

    record = {
        "task": name,
        "asana_id": gid,
        "asana_link": link,
        "asana_due": asana_due,
        "notion_due": notion_due,
        "baseline": baseline if had_baseline else None,
        "reason": "both sides changed" if had_baseline else "no baseline to compare against",
    }

    if _sticky_choice:
        record["resolution"] = _sticky_choice
        _conflicts.append(record)
        return _sticky_choice

    if not can_prompt():
        record["resolution"] = "skip"
        _conflicts.append(record)
        log.warning(
            "  CONFLICT (left unchanged): '%s' - Asana %s vs Notion %s",
            name, _fmt(asana_due), _fmt(notion_due),
        )
        return "skip"

    was = f"both were {_fmt(baseline)} at last sync" if had_baseline else "no record of a previous sync"
    print("\n" + "-" * 72)
    print(f"Due-date conflict: {name}")
    print(f"  Asana : {_fmt(asana_due)}")
    print(f"  Notion: {_fmt(notion_due)}")
    print(f"  ({was})")
    if link:
        print(f"  {link}")
    print("  [a] keep Asana   [n] keep Notion   [s] skip")
    print("  [A] keep Asana for all remaining   [N] keep Notion for all remaining")

    choice = "skip"
    while True:
        try:
            answer = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if answer in ("A", "N"):
            choice = _sticky_choice = "asana" if answer == "A" else "notion"
            break
        if answer in ("a", "n", "s"):
            choice = {"a": "asana", "n": "notion", "s": "skip"}[answer]
            break
        print("  Please enter a, n, s, A or N.")

    record["resolution"] = choice
    _conflicts.append(record)
    return choice


def choose_due_winner(
    state: dict,
    gid: str,
    name: str,
    link: str,
    asana_due: Optional[str],
    notion_due: Optional[str],
) -> str:
    """
    Decide whose due date to keep: "agree", "asana", "notion" or "skip".

    Compares both sides against the baseline from the last run. If only one
    side moved, that side wins. If both moved, or there is no baseline and both
    hold a real date, it's a conflict and resolve_due_conflict() decides.
    """
    if notion_due == asana_due:
        return "agree"

    entry = state["tasks"].get(gid, {})
    had_baseline = "due" in entry
    baseline = entry.get("due")

    if had_baseline and notion_due == baseline:
        return "asana"   # only Asana moved
    if had_baseline and asana_due == baseline:
        return "notion"  # only Notion moved
    if not had_baseline and not notion_due:
        return "asana"   # nothing in Notion to overwrite
    if not had_baseline and not asana_due:
        return "notion"  # nothing in Asana to overwrite

    return resolve_due_conflict(
        name, gid, link, asana_due, notion_due, baseline, had_baseline
    )


def write_conflict_log() -> None:
    """Persist this run's conflicts so an unattended run leaves a trail."""
    if not _conflicts or DRY_RUN:
        return
    try:
        CONFLICT_LOG.parent.mkdir(parents=True, exist_ok=True)
        CONFLICT_LOG.write_text(
            json.dumps(
                {"run": datetime.now().isoformat(timespec="seconds"), "conflicts": _conflicts},
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info("  %d conflict(s) written to %s", len(_conflicts), CONFLICT_LOG)
    except OSError as exc:
        log.error("Could not write conflict log: %s", exc)


# â”€â”€ Notion helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def n_query(db_id: str, filter_payload: Optional[dict] = None) -> list[dict]:
    """Query all pages from a Notion database, handling pagination."""
    results: list[dict] = []
    payload: dict = {"page_size": 100}
    if filter_payload:
        payload["filter"] = filter_payload
    while True:
        r = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=NOTION_HEADERS,
            json=payload,
        )
        _track("notion", payload, r)
        r.raise_for_status()
        body = r.json()
        results.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        payload["start_cursor"] = body["next_cursor"]
    return results


_dry_run_seq = 0


def n_create_page(db_id: str, props: dict) -> dict:
    global _dry_run_seq
    if DRY_RUN:
        _dry_run_seq += 1
        # Props being sent are request-shaped ({"text": {"content": ...}}),
        # not the response shape plain_text() reads.
        title = next(
            (
                "".join(t.get("text", {}).get("content", "") for t in p["title"])
                for p in props.values()
                if "title" in p
            ),
            "(untitled)",
        )
        log.info("  [dry-run] would create Notion page '%s'", title)
        return {"id": f"dry-run-{_dry_run_seq}", "url": ""}
    body = {"parent": {"database_id": db_id}, "properties": props}
    r = requests.post(
        f"{NOTION_API}/pages",
        headers=NOTION_HEADERS,
        json=body,
    )
    _track("notion", body, r)
    r.raise_for_status()
    return r.json()


def n_update_page(page_id: str, props: dict) -> dict:
    if DRY_RUN:
        log.info("  [dry-run] would update Notion page %s: %s", page_id, ", ".join(sorted(props)))
        return {}
    body = {"properties": props}
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=NOTION_HEADERS,
        json=body,
    )
    _track("notion", body, r)
    r.raise_for_status()
    return r.json()


def n_archive_page(page_id: str) -> None:
    if DRY_RUN:
        log.info("  [dry-run] would archive Notion page %s", page_id)
        return
    body = {"archived": True}
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=NOTION_HEADERS,
        json=body,
    )
    _track("notion", body, r)
    r.raise_for_status()


def n_get_blocks(page_id: str) -> list[dict]:
    """Fetch all top-level child blocks from a Notion page."""
    blocks: list[dict] = []
    params: dict = {"page_size": 100}
    while True:
        r = requests.get(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            params=params,
        )
        _track("notion", None, r)
        r.raise_for_status()
        body = r.json()
        blocks.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        params["start_cursor"] = body["next_cursor"]
    return blocks


_BLOCK_READONLY = {
    "id", "created_time", "last_edited_time", "created_by",
    "last_edited_by", "has_children", "archived", "in_trash",
    "parent", "object",
}


def _clean_block(block: dict) -> Optional[dict]:
    """Strip read-only fields from a block so it can be re-appended."""
    block_type = block.get("type")
    if not block_type or block_type == "unsupported":
        return None
    type_data = {k: v for k, v in block.get(block_type, {}).items() if k != "children"}
    return {"type": block_type, block_type: type_data}


def n_append_blocks(page_id: str, blocks: list[dict]) -> None:
    """Append cleaned blocks to a Notion page in batches of 100."""
    clean = [_clean_block(b) for b in blocks]
    clean = [b for b in clean if b is not None]
    if not clean:
        return
    if DRY_RUN:
        log.info("  [dry-run] would append %d block(s) to %s", len(clean), page_id)
        return
    for i in range(0, len(clean), 100):
        body = {"children": clean[i : i + 100]}
        r = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json=body,
        )
        _track("notion", body, r)
        r.raise_for_status()


# Notion property builders

def n_title(value: str) -> dict:
    return {"title": [{"text": {"content": value or ""}}]}

def n_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value or ""}}]}

def n_select(value: str) -> dict:
    return {"select": {"name": value}}

def n_status(value: str) -> dict:
    return {"status": {"name": value}}

def n_date(iso: Optional[str]) -> dict:
    return {"date": {"start": iso}} if iso else {"date": None}

def n_url(value: str) -> dict:
    return {"url": value}

def n_relation(page_ids: list[str]) -> dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


def plain_text(prop: dict, prop_type: str = "rich_text") -> str:
    """Extract plain text from a Notion text/title property."""
    return "".join(t.get("plain_text", "") for t in prop.get(prop_type, []))


def _heading_block(text: str) -> dict:
    return {"type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": text}}]}}


def _paragraph_block(text: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": text}}]}}


def _todo_block(text: str, checked: bool) -> dict:
    return {
        "type": "to_do",
        "to_do": {"rich_text": [{"text": {"content": text}}], "checked": checked},
    }


def build_content_blocks(notes: str, subtasks: list[dict]) -> list[dict]:
    """Build Notion blocks porting over an Asana description and subtasks (as checkboxes)."""
    blocks: list[dict] = []

    notes_lines = [line for line in (notes or "").splitlines() if line.strip()]
    if notes_lines:
        blocks.append(_heading_block("Description"))
        blocks.extend(_paragraph_block(line) for line in notes_lines)

    if subtasks:
        blocks.append(_heading_block("Subtasks"))
        blocks.extend(
            _todo_block(st.get("name", ""), bool(st.get("completed")))
            for st in subtasks
        )

    return blocks


# â”€â”€ Asana helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "PUT"],
    raise_on_status=False,
)
_asana_session = requests.Session()
_asana_session.mount("https://", HTTPAdapter(max_retries=_retry))


def a_get(path: str, params: Optional[dict] = None) -> dict:
    r = _asana_session.get(
        f"{ASANA_API}{path}", headers=ASANA_HEADERS, params=params, timeout=30
    )
    _track("asana", None, r)
    r.raise_for_status()
    return r.json()


def a_update_task_due(gid: str, due_on: Optional[str]) -> None:
    if DRY_RUN:
        log.info("  [dry-run] would set Asana task %s due date to %s", gid, due_on)
        return
    body = {"data": {"due_on": due_on}}
    r = _asana_session.put(
        f"{ASANA_API}/tasks/{gid}",
        headers=ASANA_HEADERS,
        json=body,
        timeout=30,
    )
    _track("asana", body, r)
    r.raise_for_status()


def a_update_task(gid: str, completed: bool) -> None:
    if DRY_RUN:
        log.info("  [dry-run] would set Asana task %s completed=%s", gid, completed)
        return
    body = {"data": {"completed": completed}}
    r = _asana_session.put(
        f"{ASANA_API}/tasks/{gid}",
        headers=ASANA_HEADERS,
        json=body,
        timeout=30,
    )
    _track("asana", body, r)
    r.raise_for_status()


def asana_workspace_gid() -> str:
    data = a_get("/users/me", {"opt_fields": "workspaces"})
    workspaces = data["data"].get("workspaces", [])
    if not workspaces:
        raise RuntimeError("No Asana workspaces found for this token.")
    return workspaces[0]["gid"]


def asana_fetch_incomplete_tasks(workspace_gid: str) -> list[dict]:
    """
    Fetch all incomplete tasks assigned to me, paginating as needed.

    Uses /tasks rather than /workspaces/{gid}/tasks/search: the search endpoint
    caps at 100 results and never returns a next_page, so paginating it is
    impossible and everything past the first 100 is invisible to the sync.
    completed_since=now restricts this to tasks that are still open.
    """
    tasks: list[dict] = []
    params: dict = {
        "assignee": ASANA_USER_GID,
        "workspace": workspace_gid,
        "completed_since": "now",
        "opt_fields": (
            "gid,name,due_on,projects,projects.name,permalink_url,completed,"
            "modified_at,notes,num_subtasks"
        ),
        "limit": 100,
    }
    while True:
        data = a_get("/tasks", params)
        tasks.extend(data.get("data", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        params["offset"] = next_page["offset"]
    # completed_since is a lower bound, not a filter, so drop anything already
    # closed rather than trusting the server to have excluded it.
    return [t for t in tasks if not t.get("completed")]


def asana_fetch_projects(workspace_gid: str) -> list[dict]:
    """
    Fetch every project in the workspace, paginating as needed.

    The task search only returns incomplete tasks, so a finished project has
    nothing left to surface it. Listing projects directly is the only way to
    see one that was completed or archived.
    """
    projects: list[dict] = []
    params: dict = {
        "opt_fields": "gid,name,archived,completed,completed_at,permalink_url",
        "limit": 100,
    }
    while True:
        data = a_get(f"/workspaces/{workspace_gid}/projects", params)
        projects.extend(data.get("data", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        params["offset"] = next_page["offset"]
    return projects


def asana_fetch_subtasks(gid: str) -> list[dict]:
    """Fetch all subtasks (name, completed) for an Asana task, paginating as needed."""
    subtasks: list[dict] = []
    params: dict = {"opt_fields": "name,completed", "limit": 100}
    while True:
        data = a_get(f"/tasks/{gid}/subtasks", params)
        subtasks.extend(data.get("data", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        params["offset"] = next_page["offset"]
    return subtasks


# â”€â”€ Project helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def build_project_map(pages: list[dict]) -> dict[str, dict]:
    """Return {project_name: {page_id, url}} from Notion Projects query results."""
    mapping: dict[str, dict] = {}
    for page in pages:
        name = plain_text(page["properties"].get("Project Name", {}), "title").strip()
        if name:
            mapping[name] = {"page_id": page["id"], "url": page.get("url", "")}
    return mapping


def create_notion_project(name: str) -> dict:
    """
    Create a new project page in the Notion Projects DB.

    NOTE: Adding inline Tasks + Meeting Notes views requires the Notion MCP
    create_view endpoint, which is not available in the standard Notion REST API.
    Configure those views manually in Notion after the project is created.
    """
    props = {
        "Project Name": n_title(name),
        "Project Status": n_select("In Progress"),
        "Domains": n_relation([KTAF_DOMAIN_PAGE_ID]),
    }
    page = n_create_page(PROJECTS_DB, props)
    log.info("  Created Notion project '%s' â†’ %s", name, page["id"])
    return page


# â”€â”€ Main sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def sync() -> str:
    counts = {
        "new": 0,
        "updated": 0,
        "new_projects": 0,
        "pushed": 0,
        "done_from_asana": 0,
        "due_date_pushed": 0,
        "due_date_pulled": 0,
        "due_conflicts": 0,
        "projects_closed": 0,
        "content_backfilled": 0,
    }

    state = load_state()

    # PART 0: Refresh project mapping
    log.info("Part 0: Loading Notion project map...")
    project_pages = n_query(PROJECTS_DB)
    project_map = build_project_map(project_pages)
    log.info("  %d existing projects found", len(project_map))

    # PART 1: Asana â†’ Notion (pull)
    log.info("Part 1: Fetching incomplete tasks from Asana...")
    workspace_gid = asana_workspace_gid()
    asana_tasks = asana_fetch_incomplete_tasks(workspace_gid)
    log.info("  %d incomplete Asana tasks fetched", len(asana_tasks))
    log.info("  %d tasks have project memberships", sum(1 for t in asana_tasks if t.get("projects")))

    # Ensure every Asana project name has a matching Notion project page
    all_project_names: set[str] = set()
    for task in asana_tasks:
        for proj in task.get("projects") or []:
            pname = proj.get("name")
            if pname:
                all_project_names.add(pname)

    for proj_name in sorted(all_project_names):
        if proj_name not in project_map:
            # Live check before creating â€” guards against stale startup snapshot
            live = n_query(PROJECTS_DB, {
                "property": "Project Name",
                "title": {"equals": proj_name},
            })
            if live:
                project_map[proj_name] = {"page_id": live[0]["id"], "url": live[0].get("url", "")}
                log.info("  Project '%s' found via live check (not in initial snapshot)", proj_name)
            else:
                log.info("  New project detected: '%s'", proj_name)
                try:
                    page = create_notion_project(proj_name)
                    project_map[proj_name] = {"page_id": page["id"], "url": page.get("url", "")}
                    counts["new_projects"] += 1
                except Exception as exc:
                    log.error("  Failed to create project '%s': %s", proj_name, exc)

    # Build Asana ID â†’ Notion page info map (Source = "Asana" only)
    log.info("  Querying existing Notion All Tasks (Source=Asana)...")
    notion_asana_pages = n_query(
        ALL_TASKS_DB,
        {"property": "Source", "select": {"equals": "Asana"}},
    )
    notion_map: dict[str, dict] = {}
    for page in notion_asana_pages:
        asana_id = plain_text(page["properties"].get("Asana ID", {}))
        if asana_id:
            status_name = (
                page["properties"].get("Status", {})
                .get("status", {})
                .get("name", "")
            )
            has_domains = bool(
                page["properties"].get("Domains", {}).get("relation", [])
            )
            notion_due = (
                (page["properties"].get("Due Date", {}).get("date") or {})
                .get("start")
            )
            notion_map[asana_id] = {
                "page_id": page["id"],
                "status": status_name,
                "has_domains": has_domains,
                "due_date": notion_due,
            }

    # Create or update a Notion task entry for each Asana task
    asana_gids_seen: set[str] = set()
    for task in asana_tasks:
        gid = task["gid"]
        asana_gids_seen.add(gid)
        name = task.get("name", "")
        due_on = task.get("due_on")
        permalink = task.get("permalink_url", "")
        notes = task.get("notes") or ""
        has_asana_content = bool(notes.strip()) or bool(task.get("num_subtasks"))
        project_names = [p["name"] for p in (task.get("projects") or []) if p.get("name")]
        project_str = "; ".join(project_names) if project_names else "(none)"
        project_page_ids = [
            project_map[pn]["page_id"]
            for pn in project_names
            if pn in project_map
        ]

        existing = notion_map.get(gid)

        try:
            if existing is None:
                props = {
                    "Task": n_title(name),
                    "Status": n_status("Not started"),
                    "Source": n_select("Asana"),
                    "Asana Project": n_text(project_str),
                    "Asana Link": n_url(permalink),
                    "Asana ID": n_text(gid),
                    "Last Synced": n_date(TODAY),
                    "Domains": n_relation([KTAF_DOMAIN_PAGE_ID]),
                    "Projects": n_relation(project_page_ids),
                }
                if due_on:
                    props["Due Date"] = n_date(due_on)
                new_page = n_create_page(ALL_TASKS_DB, props)
                counts["new"] += 1
                record_due_baseline(state, gid, due_on)

                if has_asana_content:
                    blocks = build_content_blocks(notes, asana_fetch_subtasks(gid))
                    if blocks:
                        n_append_blocks(new_page["id"], blocks)
                        counts["content_backfilled"] += 1

            elif existing["status"] != "Done":
                props = {
                    "Task": n_title(name),
                    "Asana Project": n_text(project_str),
                    "Asana Link": n_url(permalink),
                    "Last Synced": n_date(TODAY),
                    "Projects": n_relation(project_page_ids),
                }
                # Due dates: reconcile against what both sides agreed on last
                # run, so a change made in Asana is no longer steamrolled by
                # whatever Notion happens to hold.
                notion_due = existing.get("due_date")
                winner = choose_due_winner(state, gid, name, permalink, due_on, notion_due)

                if winner == "agree":
                    record_due_baseline(state, gid, due_on)
                elif winner == "asana":
                    props["Due Date"] = n_date(due_on)
                    counts["due_date_pulled"] += 1
                    log.info(
                        "  Due date %s in Notion for '%s': %s -> %s",
                        did("Set"), name, notion_due or "(none)", due_on or "(none)",
                    )
                    record_due_baseline(state, gid, due_on)
                elif winner == "notion":
                    a_update_task_due(gid, notion_due)
                    counts["due_date_pushed"] += 1
                    log.info(
                        "  Due date %s in Asana for '%s': %s -> %s",
                        did("Set"), name, due_on or "(none)", notion_due or "(none)",
                    )
                    record_due_baseline(state, gid, notion_due)
                else:
                    # Unresolved: leave both sides alone and keep the old
                    # baseline, so the same conflict is raised again next run.
                    counts["due_conflicts"] += 1

                # Only touch Domains if the page has none set yet
                if not existing["has_domains"]:
                    props["Domains"] = n_relation([KTAF_DOMAIN_PAGE_ID])
                n_update_page(existing["page_id"], props)
                counts["updated"] += 1

                # Retroactive backfill: only touch pages that are still empty,
                # so any existing notes in Notion (manual or otherwise) are left alone.
                if has_asana_content and not n_get_blocks(existing["page_id"]):
                    blocks = build_content_blocks(notes, asana_fetch_subtasks(gid))
                    if blocks:
                        n_append_blocks(existing["page_id"], blocks)
                        counts["content_backfilled"] += 1

            # Status == "Done" â†’ skip; don't re-open a closed task

        except Exception as exc:
            log.error("  Failed to sync task '%s' (%s): %s", name, gid, exc)

    log.info(
        "Part 1 done: %d new, %d updated, %d new projects",
        counts["new"], counts["updated"], counts["new_projects"],
    )

    # PART 2: Notion â†’ Asana (push completions)
    log.info("Part 2: Pushing Notion 'Done' tasks â†’ Asana...")
    done_pages = n_query(
        ALL_TASKS_DB,
        {
            "and": [
                {"property": "Source", "select": {"equals": "Asana"}},
                {"property": "Status", "status": {"equals": "Done"}},
            ]
        },
    )
    for page in done_pages:
        asana_id = plain_text(page["properties"].get("Asana ID", {}))
        if not asana_id:
            continue
        try:
            a_update_task(asana_id, completed=True)
            n_update_page(page["id"], {"Last Synced": n_date(TODAY)})
            counts["pushed"] += 1
            log.info("  %s Asana task %s", did("Closed"), asana_id)
        except Exception as exc:
            log.error("  Failed to close Asana task %s: %s", asana_id, exc)

    log.info("Part 2 done: %d tasks closed in Asana", counts["pushed"])

    # PART 3: Handle tasks that disappeared from the Asana pull
    log.info("Part 3: Checking tasks absent from Asana pull...")
    still_missing: list[str] = []
    for asana_id, info in notion_map.items():
        if asana_id in asana_gids_seen or info["status"] == "Done":
            continue
        try:
            task_data = a_get(f"/tasks/{asana_id}", {"opt_fields": "completed"})
            if task_data["data"].get("completed"):
                n_update_page(info["page_id"], {
                    "Status": n_status("Done"),
                    "Last Synced": n_date(TODAY),
                })
                counts["done_from_asana"] += 1
                log.info("  Marked Done (completed on Asana side): %s", asana_id)
            else:
                still_missing.append(asana_id)
        except Exception as exc:
            log.error("  Could not check task %s: %s", asana_id, exc)

    if still_missing:
        log.warning(
            "  %d task(s) in Notion not found in current Asana pull (left unchanged): %s",
            len(still_missing),
            still_missing,
        )

    log.info("Part 3 done: %d marked Done from Asana side", counts["done_from_asana"])

    # PART 4: Asana project closed -> close the matching Notion project
    log.info("Part 4: Checking for Asana projects that are completed or archived...")
    try:
        asana_projects = asana_fetch_projects(workspace_gid)
    except Exception as exc:
        log.error("  Could not list Asana projects: %s", exc)
        asana_projects = []
    log.info("  %d Asana projects fetched", len(asana_projects))

    # Notion projects carry no Asana ID, so they are matched on name, the same
    # way Part 1 matches them when creating.
    asana_by_name: dict[str, list[dict]] = {}
    for proj in asana_projects:
        pname = (proj.get("name") or "").strip()
        if pname:
            asana_by_name.setdefault(pname, []).append(proj)

    for page in project_pages:
        pname = plain_text(page["properties"].get("Project Name", {}), "title").strip()
        if not pname:
            continue
        current = (page["properties"].get("Project Status") or {}).get("select") or {}
        if current.get("name") == "Complete":
            continue
        twins = asana_by_name.get(pname)
        if not twins:
            continue
        # Require every Asana project sharing the name to be done, so a name
        # reused by a project that is still running doesn't close the page.
        if not all(t.get("completed") or t.get("archived") for t in twins):
            continue
        reason = "completed" if any(t.get("completed") for t in twins) else "archived"
        try:
            n_update_page(page["id"], {"Project Status": n_select("Complete")})
            counts["projects_closed"] += 1
            log.info("  %s Notion project '%s' (%s in Asana)", did("Closed"), pname, reason)
        except Exception as exc:
            log.error("  Failed to close Notion project '%s': %s", pname, exc)

    log.info("Part 4 done: %d project(s) closed in Notion", counts["projects_closed"])

    save_state(state)
    write_conflict_log()

    # Summary line
    total_synced = counts["new"] + counts["updated"]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary = (
        f"Synced {total_synced} tasks at {timestamp}. "
        f"Pull: {counts['new']} new, {counts['updated']} updated. "
        f"Projects: {counts['new_projects']} new (add Tasks + Meeting Notes views manually), "
        f"{counts['projects_closed']} closed from Asana. "
        f"Push: {counts['pushed']} closed in Asana. "
        f"Due dates: {counts['due_date_pulled']} pulled from Asana, "
        f"{counts['due_date_pushed']} pushed to Asana, "
        f"{counts['due_conflicts']} unresolved. "
        f"{counts['done_from_asana']} marked Done from Asana side. "
        f"{counts['content_backfilled']} page(s) backfilled with Asana description/subtasks."
    )
    if DRY_RUN:
        summary = "[DRY RUN - nothing was written] " + summary
    print(summary)
    return summary


def merge_duplicate_projects(dry_run: bool = False) -> None:
    """
    Find project pages in Notion that share the same name and merge them.

    For each duplicate group:
      1. Pick the first page as canonical.
      2. Re-point every task's Projects relation from each dupe â†’ canonical.
      3. Copy block content from each dupe into canonical (with a divider).
      4. Archive each dupe.

    Pass dry_run=True to log what would happen without making any changes.
    """
    from collections import defaultdict

    log.info("Loading all Notion projects...")
    project_pages = n_query(PROJECTS_DB)

    name_groups: dict[str, list[dict]] = defaultdict(list)
    for page in project_pages:
        name = plain_text(page["properties"].get("Project Name", {}), "title").strip()
        if name:
            name_groups[name].append(page)

    dupes = {name: pages for name, pages in name_groups.items() if len(pages) > 1}

    if not dupes:
        log.info("No duplicate projects found.")
        return

    log.info("Found %d duplicate project name(s):", len(dupes))
    for name, pages in dupes.items():
        log.info("  '%s' â€” %d copies", name, len(pages))

    for name, pages in dupes.items():
        canonical = pages[0]
        canonical_id = canonical["id"]
        log.info("\nMerging '%s' â€” keeping %s", name, canonical_id)

        for dupe in pages[1:]:
            dupe_id = dupe["id"]
            log.info("  Processing dupe: %s", dupe_id)

            # Re-point tasks
            tasks = n_query(ALL_TASKS_DB, {
                "property": "Projects",
                "relation": {"contains": dupe_id},
            })
            log.info("    %d task(s) reference this dupe", len(tasks))
            for task_page in tasks:
                current_ids = {r["id"] for r in task_page["properties"].get("Projects", {}).get("relation", [])}
                new_ids = (current_ids - {dupe_id}) | {canonical_id}
                if not dry_run:
                    n_update_page(task_page["id"], {"Projects": n_relation(list(new_ids))})
                    log.info("    Updated task %s", task_page["id"])

            # Copy block content
            blocks = n_get_blocks(dupe_id)
            if blocks:
                log.info("    Copying %d block(s) of content", len(blocks))
                if not dry_run:
                    # Prepend a divider and attribution callout so merged content is traceable
                    n_append_blocks(canonical_id, [
                        {"type": "divider", "divider": {}},
                        {
                            "type": "callout",
                            "callout": {
                                "rich_text": [{"type": "text", "text": {
                                    "content": f"Merged from duplicate page {dupe_id}"
                                }}],
                                "icon": {"type": "emoji", "emoji": "ðŸ”€"},
                                "color": "gray_background",
                            },
                        },
                    ])
                    n_append_blocks(canonical_id, blocks)

            # Archive the dupe
            log.info("    Archiving dupe %s", dupe_id)
            if not dry_run:
                n_archive_page(dupe_id)

    if dry_run:
        log.info("\nDRY RUN complete â€” no changes made.")
    else:
        log.info("\nMerge complete.")


def discover():
    """Print all Notion databases visible to this integration token."""
    print("Searching for databases accessible to your integration...\n")
    results, payload = [], {"filter": {"value": "database", "property": "object"}, "page_size": 100}
    while True:
        r = requests.post(f"{NOTION_API}/search", headers=NOTION_HEADERS, json=payload)
        _track("notion", payload, r)
        r.raise_for_status()
        body = r.json()
        results.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        payload["start_cursor"] = body["next_cursor"]

    if not results:
        print("No databases found. Make sure you connected the integration to your databases in Notion.")
        return

    for db in results:
        title_parts = db.get("title", [])
        title = "".join(t.get("plain_text", "") for t in title_parts) or "(untitled)"
        print(f"  {title}")
        print(f"    ID : {db['id']}")
        print(f"    URL: {db.get('url', '')}")
        print()


USAGE = """Usage: python asana_notion_sync.py [command] [options]

Commands:
  (none)          Run the full two-way sync.
  --discover      List the Notion databases this integration can see.
  --merge-dupes   Merge Notion project pages that share a name.

Options:
  --dry-run       Report what would change without writing anything.
  --no-prompt     Never ask about conflicts; log them and leave both sides alone.
  -h, --help      Show this message.
"""


if __name__ == "__main__":
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print(USAGE)
        sys.exit(0)

    DRY_RUN = "--dry-run" in args
    ALLOW_PROMPT = "--no-prompt" not in args

    if DRY_RUN:
        log.info("DRY RUN mode - no changes will be made")

    if "--discover" in args:
        discover()
    elif "--merge-dupes" in args:
        merge_duplicate_projects(dry_run=DRY_RUN)
    else:
        if not ALLOW_PROMPT:
            log.info("Prompting disabled - conflicts will be logged, not resolved")
        elif not can_prompt():
            log.info("No terminal attached - conflicts will be logged, not resolved")
        sync()
    print_api_stats()
