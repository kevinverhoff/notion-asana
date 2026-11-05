# notion-asana

Bidirectional sync between Asana tasks and a Notion "All Tasks" database.

## What it does

The script runs in four parts each time it executes:

**Part 1 — Asana → Notion (pull):** Fetches all incomplete Asana tasks assigned to you and creates or updates matching pages in your Notion "All Tasks" database. Tasks are tagged with their Asana project, a direct link back to Asana, and a last-synced date. If an Asana project doesn't yet exist in your Notion Projects database, it's created automatically.

**Part 2 — Notion → Asana (push completions):** Finds tasks marked "Done" in Notion and marks them complete in Asana.

**Part 3 — Drift detection:** For tasks that were in Notion but disappeared from the Asana pull, the script checks Asana directly. If the task was completed there, it marks it Done in Notion. Tasks that can't be explained are logged as warnings.

**Part 4 — Closing projects:** Lists every Asana project and closes the matching Notion project page (sets **Project Status = Complete**) when its Asana counterpart is completed *or* archived. Projects are matched by name, the same way Part 1 matches them. If several Asana projects share a name, the Notion page is only closed when all of them are done, so a name reused by a live project doesn't close it. Closing is one-way: reopening a project in Asana never reopens it in Notion.

## Due dates

Due dates move in both directions. Which side wins is decided by comparing both against a **baseline** — the value the two sides agreed on at the end of the last run, stored in `.sync_state.json`.

| At last sync | Asana now | Notion now | Result |
|---|---|---|---|
| Nov 10 | **Nov 12** | Nov 10 | Only Asana moved → Notion is updated to Nov 12 |
| Nov 10 | Nov 10 | **Nov 20** | Only Notion moved → Asana is updated to Nov 20 |
| Nov 10 | **Nov 12** | **Nov 20** | Both moved → conflict |
| *(no record)* | Nov 12 | *(empty)* | Nothing to overwrite → Notion gets Nov 12 |
| *(no record)* | Nov 12 | Nov 20 | Can't tell who moved → conflict |

Clearing a date counts as a change, so deleting a due date in Asana clears it in Notion too.

**Conflicts** are only raised when both sides genuinely moved. Running in a terminal, you're asked about each one:

```
Due-date conflict: Draft Q2 board memo
  Asana : 2026-11-12
  Notion: 2026-11-20
  (both were 2026-11-10 at last sync)
  https://app.asana.com/...
  [a] keep Asana   [n] keep Notion   [s] skip
  [A] keep Asana for all remaining   [N] keep Notion for all remaining
```

Skipped conflicts leave both sides untouched and keep the old baseline, so the same question comes back next run until you resolve it. With no terminal attached (cron, launchd) or with `--no-prompt`, nothing is asked and nothing is overwritten — conflicts are logged as warnings and written to `logs/conflicts.json`.

### The state file

`.sync_state.json` holds only the last-agreed due date per Asana task ID. It's gitignored and safe to delete: the next run just rebuilds it, treating anything where both sides hold different real dates as a conflict to ask about. Because it's local, syncing from a second machine starts without a baseline.

## Prerequisites

- Python 3.11+
- An Asana [Personal Access Token](https://developers.asana.com/docs/personal-access-token)
- A Notion [internal integration token](https://www.notion.so/my-integrations) connected to your All Tasks and Projects databases

## Installation

```bash
git clone https://github.com/your-org/notion-asana.git
cd notion-asana
pip install requests python-dotenv
```

## Configuration

### 1. Create a `.env` file

```dotenv
ASANA_TOKEN=1/1234567890123456:abcdef1234567890abcdef1234567890
NOTION_TOKEN=secret_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890abcd
```

### 2. Update the static IDs in the script

Open `asana_notion_sync.py` and set the following constants to match your Notion workspace:

| Constant | Description |
|---|---|
| `ALL_TASKS_DB` | ID of your Notion "All Tasks" database |
| `PROJECTS_DB` | ID of your Notion Projects database |
| `ASANA_USER_GID` | Your Asana user GID (find it in your Asana profile URL) |
| `KTAF_DOMAIN_PAGE_ID` | ID of a Notion page to use as the default Domains relation |

Run with `--discover` to list all databases your integration can see (see Usage below).

### 3. Connect the Notion integration

In Notion, open each database → **...** menu → **Connections** → add your integration. The script cannot read or write databases it is not connected to.

## Usage

**Normal sync:**

```bash
python asana_notion_sync.py
```

**Preview a sync without writing anything:**

```bash
python asana_notion_sync.py --dry-run
```

Every change is logged as `[dry-run] would ...` and neither API is written to. Worth running first after any change to the script or your databases.

**Run unattended, never asking about conflicts:**

```bash
python asana_notion_sync.py --no-prompt
```

**Full option list:**

```bash
python asana_notion_sync.py --help
```

**Discover accessible Notion databases** (useful for finding IDs during setup):

```bash
python asana_notion_sync.py --discover
```

**Merge duplicate project pages in Notion:**

```bash
# Preview what would change (no edits made)
python asana_notion_sync.py --merge-dupes --dry-run

# Apply the merge
python asana_notion_sync.py --merge-dupes
```

The merge command finds project pages with the same name, keeps the first one as canonical, re-points all task relations to it, copies over any page content, and archives the duplicates.

## Scheduling

The script is designed to run at 8 AM and 5 PM daily. On macOS/Linux, add a cron job:

```cron
0 8,17 * * * /path/to/notion-asana/run_sync.sh
```

`run_sync.sh` resolves its own directory, builds the venv on first run, and appends to `logs/sync.log`. On Windows, use Task Scheduler pointing at the script.

Under cron there is no terminal, so due-date conflicts are never resolved automatically — they're logged to `logs/conflicts.json`. Re-run by hand to work through them.

## Tests

```bash
python test_due_merge.py
```

Covers the due-date decision table and the conflict prompt. No API calls are made, so it's safe to run any time.

## Notes

- Tasks synced from Asana are tagged `Source = Asana` in Notion. The script only manages pages with that tag — it will not touch tasks you created directly in Notion.
- When a new Asana project is auto-created in Notion, you will need to manually add the Tasks and Meeting Notes inline views — this is not available via the Notion REST API.
- The `.env` file is excluded from version control via `.gitignore`. Never commit your tokens.
- Open tasks are fetched from Asana's `/tasks` endpoint rather than `/tasks/search`. The search endpoint silently caps at 100 results and returns no pagination cursor, so anything past the first 100 was previously invisible to the sync.
