#!/usr/bin/env python3
"""
Asana <-> Notion bidirectional task sync.

Pulls new/changed Asana tasks into the Notion "All Tasks" database.
Closes Asana tasks that are marked Done in Notion.
Schedule at 8 AM and 5 PM daily.

Usage:
    Create a .env file with:
        ASANA_TOKEN=<personal access token>
        NOTION_TOKEN=<integration token>
    python asana_notion_sync.py

Dependencies:
    pip install requests python-dotenv
"""

import os
import sys
import logging
from datetime import date, datetime
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
        r.raise_for_status()
        body = r.json()
        results.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        payload["start_cursor"] = body["next_cursor"]
    return results


def n_create_page(db_id: str, props: dict) -> dict:
    r = requests.post(
        f"{NOTION_API}/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": db_id}, "properties": props},
    )
    r.raise_for_status()
    return r.json()


def n_update_page(page_id: str, props: dict) -> dict:
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"properties": props},
    )
    r.raise_for_status()
    return r.json()


def n_archive_page(page_id: str) -> None:
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"archived": True},
    )
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
    for i in range(0, len(clean), 100):
        r = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": clean[i : i + 100]},
        )
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
    r.raise_for_status()
    return r.json()


def a_update_task_due(gid: str, due_on: Optional[str]) -> None:
    r = _asana_session.put(
        f"{ASANA_API}/tasks/{gid}",
        headers=ASANA_HEADERS,
        json={"data": {"due_on": due_on}},
        timeout=30,
    )
    r.raise_for_status()


def a_update_task(gid: str, completed: bool) -> None:
    r = _asana_session.put(
        f"{ASANA_API}/tasks/{gid}",
        headers=ASANA_HEADERS,
        json={"data": {"completed": completed}},
        timeout=30,
    )
    r.raise_for_status()


def asana_workspace_gid() -> str:
    data = a_get("/users/me", {"opt_fields": "workspaces"})
    workspaces = data["data"].get("workspaces", [])
    if not workspaces:
        raise RuntimeError("No Asana workspaces found for this token.")
    return workspaces[0]["gid"]


def asana_fetch_incomplete_tasks(workspace_gid: str) -> list[dict]:
    """Fetch all incomplete tasks assigned to me, paginating as needed."""
    tasks: list[dict] = []
    params: dict = {
        "assignee.any": ASANA_USER_GID,
        "completed": "false",
        "opt_fields": "gid,name,due_on,projects,projects.name,permalink_url,completed,modified_at",
        "limit": 100,
    }
    while True:
        data = a_get(f"/workspaces/{workspace_gid}/tasks/search", params)
        tasks.extend(data.get("data", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        params["offset"] = next_page["offset"]
    return tasks


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
    }

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
                n_create_page(ALL_TASKS_DB, props)
                counts["new"] += 1

            elif existing["status"] != "Done":
                props = {
                    "Task": n_title(name),
                    "Asana Project": n_text(project_str),
                    "Asana Link": n_url(permalink),
                    "Last Synced": n_date(TODAY),
                    "Projects": n_relation(project_page_ids),
                }
                notion_due = existing.get("due_date")
                if notion_due and notion_due != due_on:
                    # Notion has a date that differs from Asana â†’ push to Asana
                    a_update_task_due(gid, notion_due)
                    counts["due_date_pushed"] += 1
                    log.info(
                        "  Due date pushed to Asana for '%s': %s â†’ %s",
                        name, due_on, notion_due,
                    )
                elif not notion_due and due_on:
                    # Notion has no date but Asana does â†’ populate Notion
                    props["Due Date"] = n_date(due_on)
                # Only touch Domains if the page has none set yet
                if not existing["has_domains"]:
                    props["Domains"] = n_relation([KTAF_DOMAIN_PAGE_ID])
                n_update_page(existing["page_id"], props)
                counts["updated"] += 1

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
            log.info("  Closed Asana task %s", asana_id)
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

    # Summary line
    total_synced = counts["new"] + counts["updated"]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary = (
        f"Synced {total_synced} tasks at {timestamp}. "
        f"Pull: {counts['new']} new, {counts['updated']} updated. "
        f"Projects: {counts['new_projects']} new (add Tasks + Meeting Notes views manually). "
        f"Push: {counts['pushed']} closed in Asana, {counts['due_date_pushed']} due dates updated in Asana. "
        f"{counts['done_from_asana']} marked Done from Asana side."
    )
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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--discover":
        discover()
    elif len(sys.argv) > 1 and sys.argv[1] == "--merge-dupes":
        dry_run = "--dry-run" in sys.argv
        if dry_run:
            log.info("DRY RUN mode â€” no changes will be made")
        merge_duplicate_projects(dry_run=dry_run)
    else:
        sync()
