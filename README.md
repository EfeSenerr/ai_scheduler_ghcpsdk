# 🤖 AI Scheduler — Multi-Page Change Monitor

An autonomous AI agent that monitors **multiple webpages** for content changes and sends email notifications with screenshots when differences are detected.

Built with the **GitHub Copilot SDK**, **Playwright MCP**, and **GitHub Actions** — the entire agent runs serverlessly on a cron schedule with zero infrastructure to manage.

---

## How It Works

```
GitHub Actions (daily cron) or local .exe
  │
  ▼
┌─────────────────────────────────────────────────┐
│  For each page in pages.json:                   │
│                                                 │
│  Copilot SDK Session (gpt-5.4)                  │
│                                                 │
│  1. web_fetch → get page content                │
│  2. CompareContentOfPage → SHA-256 diff         │
│  3. Decision: changed?                          │
│  4. Playwright MCP → full-page screenshot       │
│  5. SendMailTo → SMTP email + attachment        │
│  6. ReportResult → structured output            │
└─────────────────────────────────────────────────┘
  │
  ▼
Per-page snapshots persisted on `data` branch
```

Pages are configured in `pages.json`. Each page gets its own Copilot session, snapshot file (`snapshots/{id}.txt`), and per-page screenshot. The agent iterates through all enabled pages sequentially.

The AI agent **reasons through** a structured prompt and decides autonomously whether changes are meaningful (ignoring whitespace-only diffs). When real content changes are found, it takes a screenshot of the page via a headless browser, summarizes the diff, and emails the notification.

---

## Architecture

### Copilot SDK

For each page, the agent creates a dedicated session with per-page tools:

```python
for page in load_pages():
    compare_tool = make_compare_tool(page["id"])   # bound to snapshots/{id}.txt
    sendmail_tool = make_sendmail_tool(page["id"])  # looks for screenshots/{id}.png

    session = await client.create_session({
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "streaming": True,
        "tools": [compare_tool, sendmail_tool, ReportResult],
        "mcp_servers": {
            "playwright": {
                "type": "local",
                "command": "npx",
                "args": ["-y", "@playwright/mcp@latest", "--headless", "--output-dir", ...],
                "tools": ["browser_navigate", "browser_take_screenshot"],
            },
        },
    })
    await session.send({"prompt": _build_prompt(page)})
```

### Built-in Tools

By default, the Copilot SDK operates with `--allow-all`, enabling all first-party tools from the Copilot CLI. This gives the agent access to a rich set of capabilities out of the box:

| Category | Tools | Description |
|----------|-------|-------------|
| **File System** | `view`, `edit`, `create_file`, `glob` | Read, write, create, and find files in the working directory |
| **Shell** | `bash` | Execute shell commands |
| **Search** | `grep` | Search file contents with pattern matching |
| **Web** | `web_fetch` | Fetch webpage content via HTTP (used by this agent for page monitoring) |
| **User Interaction** | `ask_user` | Request input from the user (enabled via `on_user_input_request` handler) |

You can control which tools are available using `available_tools` (whitelist) or `excluded_tools` (blacklist) in the session config. Setting `available_tools: []` disables all built-in tools — useful when you want to provide only custom tools.

This agent uses `web_fetch` to retrieve page content and relies on three additional custom tools for its domain-specific logic.

### Custom Tools (`@define_tool`)

| Tool | Purpose |
|------|---------|
| `CompareContentOfPage` | Per-page tool — compares current page text against `snapshots/{id}.txt` using SHA-256 hashing and unified diff |
| `SendMailTo` | Per-page tool — sends email notifications via SMTP with optional screenshot attachment (`screenshots/{id}.png`) |
| `ReportResult` | Returns a structured Pydantic result (changes detected, added/removed lines, email status) |

### MCP Server

The **Playwright MCP server** runs as a local process and exposes two whitelisted tools to the agent:

- `browser_navigate` — opens the URL in a headless Chromium browser
- `browser_take_screenshot` — captures a full-page screenshot for the email

Content fetching uses the built-in `web_fetch` tool (not the browser), keeping browser usage strictly for screenshots.

### State Management

Page snapshots are stored in the `snapshots/` directory — one file per page (`snapshots/{id}.txt`). These are persisted on a separate `data` branch in the repository, keeping `main` clean while giving the agent persistent state across runs.

### Page Configuration

Pages are configured in `pages.json` at the project root:

```json
[
  {
    "id": "qatar_travel_alerts",
    "name": "Qatar Airways Travel Alerts",
    "url": "https://www.qatarairways.com/en/travel-alerts.html",
    "prompt_context": "Focus on flight disruptions affecting MUC → DOH → MCT on 01.04.2026.",
    "enabled": true
  },
  {
    "id": "another_page",
    "name": "Another Page to Monitor",
    "url": "https://example.com/status",
    "prompt_context": "Watch for pricing changes or service interruptions.",
    "enabled": true
  }
]
```

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique slug — used for snapshot filename and screenshot |
| `name` | Yes | Human-readable name — used in email subjects and logs |
| `url` | Yes | URL to monitor |
| `prompt_context` | No | Custom instructions telling the AI what to focus on |
| `enabled` | No | Set to `false` to skip (defaults to `true`) |

---

## Fork & Customize

### 1. Fork the Repository

Click **Fork** on GitHub to create your own copy.

### 2. Create the `data` Branch

```bash
git checkout --orphan data
git rm -rf .
git commit --allow-empty -m "init data branch"
git push origin data
```

### 3. Configure Pages

Edit `pages.json` to add the pages you want to monitor:

```json
[
  {
    "id": "my_page",
    "name": "My Important Page",
    "url": "https://example.com/status",
    "prompt_context": "Watch for service disruptions or pricing changes.",
    "enabled": true
  }
]
```

You can add as many pages as you need. Each page gets its own AI session, snapshot, and email notifications.

> **Backwards compatible:** If `pages.json` doesn't exist, the agent falls back to the `PAGE_URL` environment variable for single-page mode.

### 4. Configure Email (SMTP)

Add these **repository secrets** under **Settings → Secrets and variables → Actions**:

| Secret | Description | Example |
|--------|-------------|---------|
| `MAIL_SERVER` | SMTP server hostname | `smtp.gmail.com` |
| `MAIL_PORT` | SMTP port (TLS) | `587` |
| `MAIL_USERNAME` | SMTP login username | `you@gmail.com` |
| `MAIL_PASSWORD` | SMTP login password or app password | `abcd efgh ijkl mnop` |
| `MAIL_FROM` | Sender address | `you@gmail.com` |
| `NOTIFY_EMAIL` | Recipient address(es) | `alert@example.com` |

> **Gmail users:** Use an [App Password](https://support.google.com/accounts/answer/185833) instead of your regular password.

### 5. Configure the Copilot Token

Add a `COPILOT_TOKEN` secret with a GitHub token that has Copilot access.

| Secret | Description |
|--------|-------------|
| `COPILOT_TOKEN` | GitHub token with Copilot API access |

#### Why is the Copilot CLI required?

The Python package (`from copilot import CopilotClient`) is a thin wrapper — it does **not** bundle the Copilot runtime itself. Instead, it communicates with a **local CLI binary** (`@github/copilot`) that handles authentication, token exchange, and the actual API calls to the Copilot backend. Without the CLI binary present, `CopilotClient` has no way to reach the Copilot service.

That's why the workflow (and local setup) both run:

```bash
npm install @github/copilot @playwright/mcp@latest
```

The `@github/copilot` npm package contains the platform-specific CLI binary. In GitHub Actions the path is set explicitly via `COPILOT_CLI_PATH` so the Python SDK knows where to find it:

```yaml
COPILOT_CLI_PATH: ${{ github.workspace }}/node_modules/@github/copilot-linux-x64/copilot
```

On macOS/local dev the SDK auto-detects the binary, so `COPILOT_CLI_PATH` is optional.

### 6. Adjust the Schedule

Edit `.github/workflows/checkPageChanges.yml` to change the cron frequency:

```yaml
on:
  schedule:
    - cron: "*/5 * * * *"   # Every 5 minutes
    # - cron: "0 * * * *"   # Every hour
    # - cron: "0 9 * * *"   # Daily at 9 AM UTC
```

### 7. Customize Behavior (Optional)

Each page's behavior is customized via the `prompt_context` field in `pages.json` — this tells the AI what to focus on when analyzing changes.

For deeper customization, the `_build_prompt()` function in `checkPageChanges.py` controls:

- How summaries are structured
- What counts as a "meaningful" change
- Email subject line format (`🔔 Change Detected — {page name}`)
- The step-by-step workflow the agent follows

---

## Run Locally

```bash
# Clone and set up
git clone https://github.com/<your-username>/ai_scheduler.git
cd ai_scheduler
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install @github/copilot @playwright/mcp@latest
npx playwright install chrome

# Configure environment
cp .env.example .env   # or create .env manually
# Add: COPILOT_TOKEN (or GITHUB_TOKEN), MAIL_* variables

# Configure pages to monitor
# Edit pages.json with your URLs

# Run all pages
python checkPageChanges.py

# Run a single page by ID
python checkPageChanges.py --page qatar_travel_alerts
```

### Windows Desktop Launcher

A pre-built `AIScheduler.exe` is available for one-click execution. It activates the venv, runs the agent, and keeps the console open so you can read results. Pin it to your taskbar for daily use.

To rebuild after changing `launcher.py`:
```bash
pyinstaller --onefile --console --name "AIScheduler" --icon="alert_monitor.ico" launcher.py
copy dist\AIScheduler.exe .
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `COPILOT_TOKEN` or `GITHUB_TOKEN` | Yes | GitHub token with Copilot access |
| `MAIL_SERVER` | Yes | SMTP server hostname |
| `MAIL_PORT` | Yes | SMTP port |
| `MAIL_USERNAME` | Yes | SMTP username |
| `MAIL_PASSWORD` | Yes | SMTP password |
| `MAIL_FROM` | No | Sender address (defaults to `MAIL_USERNAME`) |
| `NOTIFY_EMAIL` | Yes | Comma-separated recipient email(s) |
| `COPILOT_CLI_PATH` | No | Path to Copilot CLI binary (auto-detected if not set) |

---

## Project Structure

```
ai_scheduler/
├── checkPageChanges.py           # AI agent — tools, prompt, and multi-page loop
├── pages.json                    # Page configuration (URLs, prompts, enabled flags)
├── requirements.txt              # Python dependencies
├── snapshots/                    # Per-page snapshots (persisted on data branch)
│   └── {page_id}.txt            #   e.g., qatar_travel_alerts.txt
├── screenshots/                  # Per-page Playwright screenshots (gitignored)
│   └── {page_id}.png            #   e.g., qatar_travel_alerts.png
├── launcher.py                   # Windows .exe launcher source
├── generate_icon.py              # Icon generator for the .exe
├── alert_monitor.ico             # App icon
└── .github/
    └── workflows/
        └── checkPageChanges.yml  # GitHub Actions cron workflow
```

---

## How the Diff Works

1. The agent fetches the page content via `web_fetch` (plain text, no browser)
2. `CompareContentOfPage` normalizes whitespace, computes SHA-256 hashes of old and new content
3. If hashes differ → generates a unified diff, categorizes lines as **removed** or **added**
4. The snapshot file (`snapshots/{id}.txt`) is updated immediately
5. The AI decides: if only whitespace changed → no notification. If real content changed → screenshot + email

Each page is fully independent — a failure on one page does not affect the others.

---

## License

MIT
