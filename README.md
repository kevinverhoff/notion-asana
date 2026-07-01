# notion-asana

Bidirectional sync between Asana tasks and a Notion "All Tasks" database.

## What it does

The script runs in three parts each time it executes:

**Part 1 — Asana → Notion (pull):** Fetches all incomplete Asana tasks assigned to you and creates or updates matching pages in your Notion "All Tasks" database. Tasks are tagged with their Asana project, a direct link back to Asana, and a last-synced date. If an Asana project doesn't yet exist in your Notion Projects database, it's created automatically.

**Part 2 — Notion → Asana (push completions):** Finds tasks marked "Done" in Notion and marks them complete in Asana.

**Part 3 — Drift detection:** For tasks that were in Notion but disappeared from the Asana pull, the script checks Asana directly. If the task was completed there, it marks it Done in Notion. Tasks that can't be explained are logged as warnings.

**Due date sync:** If a task has a due date in Notion that differs from Asana, the Notion date wins and gets pushed to Asana. If Asana has a date but Notion doesn't, Notion gets populated.

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

**Normal sync (run this on a schedule):**

```bash
python asana_notion_sync.py
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
0 8,17 * * * cd /path/to/notion-asana && python asana_notion_sync.py >> sync.log 2>&1
```

On Windows, use Task Scheduler pointing to the same command.

## Notes

- Tasks synced from Asana are tagged `Source = Asana` in Notion. The script only manages pages with that tag — it will not touch tasks you created directly in Notion.
- When a new Asana project is auto-created in Notion, you will need to manually add the Tasks and Meeting Notes inline views — this is not available via the Notion REST API.
- The `.env` file is excluded from version control via `.gitignore`. Never commit your tokens.
