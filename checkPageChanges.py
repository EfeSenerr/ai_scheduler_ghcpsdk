"""
Page Change Checker — Multi-URL AI Scheduler

AI agent that monitors multiple webpages for content changes and sends
email notifications when differences are detected.

Pages are configured in pages.json. Each page gets its own Copilot session,
snapshot file (snapshots/{id}.txt), and per-page screenshot.

Uses the GitHub Copilot SDK with custom tools:
  - CompareContentOfPage: compares current page content against a stored snapshot
  - SendMailTo: sends email notifications via SMTP (with optional screenshot attachment)
  - ReportResult: structured result reporting

Uses the Playwright MCP server EXCLUSIVELY for screenshots (NOT for content fetching):
  - browser_navigate: opens a URL in a headless browser
  - browser_take_screenshot: captures a full-page screenshot for email attachment
  Content fetching uses web_fetch (built-in Copilot tool).
"""

import asyncio
import difflib
import hashlib
import json
import logging
import os
import smtplib
import sys
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Optional

from copilot import CopilotClient, PermissionHandler
from copilot.tools import define_tool
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

SNAPSHOTS_DIR = Path("snapshots")
SCREENSHOT_DIR = Path("screenshots")
PAGES_FILE = Path("pages.json")
MODEL = "gpt-5.4"
REASONING_EFFORT = "high"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config loading
# ---------------------------------------------------------------------------

def load_pages() -> list[dict]:
    """Load page configurations from pages.json, with env-var fallback."""
    if PAGES_FILE.exists():
        with open(PAGES_FILE, encoding="utf-8") as f:
            pages = json.load(f)
        enabled = [p for p in pages if p.get("enabled", True)]
        log.info("Loaded %d page(s) from %s (%d enabled)", len(pages), PAGES_FILE, len(enabled))
        return enabled

    # Backwards compatibility: single URL from env var
    url = os.getenv("PAGE_URL")
    if url:
        log.info("No pages.json found — using PAGE_URL env var: %s", url)
        return [{
            "id": "default",
            "name": "Default Page",
            "url": url,
            "prompt_context": "",
            "enabled": True,
        }]

    log.error("No pages.json and no PAGE_URL env var set. Nothing to monitor.")
    return []


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------

class AgentResult(BaseModel):
    """Structured result returned by the monitoring agent."""
    changes_detected: bool = Field(description="Whether content changes were detected")
    removed: list[str] = Field(default_factory=list, description="Removed content")
    added: list[str] = Field(default_factory=list, description="New or changed content")
    summary: str = Field(description="Brief summary of the monitoring result")
    email_sent: bool = Field(default=False, description="Whether an email notification was sent")
    email_recipients: Optional[list[str]] = Field(default=None, description="Recipients, if email was sent")


@define_tool(description="Reports the structured final result of the monitoring run. MUST be called as the last step.")
async def ReportResult(params: AgentResult) -> str:  # noqa: N802
    """Receive and log the structured agent result."""
    log.info("=== Agent Result ===")
    log.info("Changes detected: %s", params.changes_detected)
    if params.removed:
        log.info("Removed:    %s", params.removed)
    if params.added:
        log.info("Added:      %s", params.added)
    log.info("Summary: %s", params.summary)
    log.info("Email sent: %s", params.email_sent)
    if params.email_recipients:
        log.info("Recipients: %d", len(params.email_recipients))
    return "Result reported successfully."


# ---------------------------------------------------------------------------
# Tool factories — create per-page tool instances
# ---------------------------------------------------------------------------

def make_compare_tool(page_id: str):
    """Create a CompareContentOfPage tool bound to a specific page snapshot."""
    snapshot_file = SNAPSHOTS_DIR / f"{page_id}.txt"

    class CompareParams(BaseModel):
        current_content: str = Field(description="The current text content of the page")

    @define_tool(
        description=(
            f"Compares the page content against the stored snapshot "
            f"({snapshot_file}) and shows differences."
        )
    )
    async def CompareContentOfPage(params: CompareParams) -> str:  # noqa: N802
        """Compare the current page content against the stored snapshot."""
        log.info("Received content: %d characters", len(params.current_content))

        def _normalize(text: str) -> str:
            lines = [line.strip() for line in text.splitlines()]
            return "\n".join(line for i, line in enumerate(lines)
                             if line or (i > 0 and lines[i - 1]))

        if not snapshot_file.exists():
            log.warning("No snapshot found (%s)", snapshot_file)
            snapshot_file.write_text(params.current_content, encoding="utf-8")
            log.info("Initial snapshot saved (%d characters)", len(params.current_content))
            return (
                "No previous snapshot found. This is the first run. "
                "Snapshot has been saved."
            )

        previous_text = snapshot_file.read_text(encoding="utf-8")
        log.info("Snapshot loaded: %d characters", len(previous_text))

        current_hash = hashlib.sha256(_normalize(params.current_content).encode()).hexdigest()
        previous_hash = hashlib.sha256(_normalize(previous_text).encode()).hexdigest()
        log.info("Hash current:  %s…", current_hash[:16])
        log.info("Hash snapshot: %s…", previous_hash[:16])

        if current_hash == previous_hash:
            log.info("No changes.")
            return "No changes detected. The content is identical to the last snapshot."

        snapshot_file.write_text(params.current_content, encoding="utf-8")
        log.info("Changes detected! Snapshot updated.")

        diff_lines = list(difflib.unified_diff(
            _normalize(previous_text).splitlines(),
            _normalize(params.current_content).splitlines(),
            fromfile="Previous",
            tofile="Current",
            lineterm="",
        ))
        removed = [l[1:].strip() for l in diff_lines if l.startswith("-") and not l.startswith("---") and l[1:].strip()]
        added = [l[1:].strip() for l in diff_lines if l.startswith("+") and not l.startswith("+++") and l[1:].strip()]

        diff_summary = "Changes detected!\n\n"
        if removed:
            diff_summary += "REMOVED:\n" + "\n".join(f"  - {l}" for l in removed) + "\n\n"
        if added:
            diff_summary += "ADDED/CHANGED:\n" + "\n".join(f"  + {l}" for l in added) + "\n"

        return diff_summary

    return CompareContentOfPage


def make_sendmail_tool(page_id: str):
    """Create a SendMailTo tool that looks for per-page screenshots."""

    class SendMailParams(BaseModel):
        to: str = Field(description="Recipient email address(es), comma-separated for multiple")
        subject: str = Field(description="Email subject line")
        body: str = Field(description="Email body (plain text)")
        screenshot_filename: Optional[str] = Field(
            default=None,
            description="Screenshot filename (in the screenshots/ folder) to attach to the email",
        )

    @define_tool(description="Sends an email notification via SMTP. Optionally with a screenshot attachment.")
    async def SendMailTo(params: SendMailParams) -> str:  # noqa: N802
        """Send an email notification via SMTP, optionally with a screenshot attachment."""
        server_addr = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
        port = int(os.environ.get("MAIL_PORT", "587"))
        username = os.environ.get("MAIL_USERNAME", "")
        password = os.environ.get("MAIL_PASSWORD", "")
        mail_from = os.environ.get("MAIL_FROM", username)

        notify_email = os.environ.get("NOTIFY_EMAIL", "")
        raw = notify_email if notify_email else params.to
        recipients = [addr.strip() for addr in raw.split(",") if addr.strip()]
        log.info("Sending email to %d recipients", len(recipients))
        log.info("Subject: %s", params.subject)

        screenshot_path = None
        if params.screenshot_filename:
            candidates = [
                SCREENSHOT_DIR / params.screenshot_filename,
                Path(params.screenshot_filename),
            ]
            for candidate in candidates:
                if candidate.exists():
                    screenshot_path = candidate
                    break
            if screenshot_path is None:
                log.warning("Screenshot not found in: %s", [str(c) for c in candidates])

        if screenshot_path:
            msg = MIMEMultipart()
            msg.attach(MIMEText(params.body, "plain", "utf-8"))
            with open(screenshot_path, "rb") as f:
                img_part = MIMEBase("image", "png")
                img_part.set_payload(f.read())
                encoders.encode_base64(img_part)
                img_part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={screenshot_path.name}",
                )
                msg.attach(img_part)
            log.info("Screenshot attached: %s", screenshot_path.name)
        else:
            msg = MIMEText(params.body, "plain", "utf-8")

        msg["Subject"] = params.subject
        msg["From"] = mail_from
        msg["To"] = ", ".join(recipients)

        with smtplib.SMTP(server_addr, port) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)

        log.info("Email successfully sent to %d recipients!", len(recipients))
        return f"Email successfully sent to {len(recipients)} recipients."

    return SendMailTo


# ---------------------------------------------------------------------------
# Agent prompt
# ---------------------------------------------------------------------------

def _build_prompt(page: dict) -> str:
    """Build the agent instruction prompt for a specific page."""
    url = page["url"]
    name = page["name"]
    prompt_context = page.get("prompt_context", "")
    page_id = page["id"]
    screenshot_filename = f"{page_id}.png"

    notify_email = os.environ.get("NOTIFY_EMAIL", "")
    if not notify_email:
        log.warning("NOTIFY_EMAIL is not set – the agent cannot send emails.")

    context_block = ""
    if prompt_context:
        context_block = f"\nCONTEXT: {prompt_context}\n"

    return (
        # --- ROLE ---
        "<role>\n"
        "You are a deterministic monitoring agent. You execute exactly the steps described below, "
        "without deviation, without improvisation.\n"
        f"You are monitoring: {name}\n"
        f"URL: {url}\n"
        f"{context_block}"
        "</role>\n\n"

        # --- ALLOWED TOOLS ---
        "<allowed_tools>\n"
        "You may ONLY use the following tools:\n"
        "  1. web_fetch          → Fetch page content as text\n"
        "  2. CompareContentOfPage → Compare text against snapshot\n"
        "  3. browser_navigate    → Open URL in browser (only for screenshots)\n"
        "  4. browser_take_screenshot → Take screenshot (only after browser_navigate)\n"
        "  5. SendMailTo          → Send email\n"
        "  6. ReportResult        → Report structured result\n\n"
        "FORBIDDEN: bash, view, create, browser_snapshot and all other tools.\n"
        "</allowed_tools>\n\n"

        # --- TOOL RULES ---
        "<tool_rules>\n"
        "- Fetch page content    → ALWAYS use web_fetch\n"
        "- Take screenshot       → ALWAYS use browser_navigate + browser_take_screenshot\n"
        "- Read/write snapshot   → handled automatically by CompareContentOfPage\n"
        "</tool_rules>\n\n"

        # --- WORKFLOW ---
        "<workflow>\n"
        "Execute these steps in order:\n\n"

        f"STEP 1: Fetch page content\n"
        f"  Call web_fetch with the URL: {url}\n"
        "  Save the returned text for Step 2.\n\n"

        "STEP 2: Compare content\n"
        "  Call CompareContentOfPage with the text from Step 1 as current_content.\n"
        "  Note the result.\n\n"

        "STEP 3: Decision\n"
        "  Check the result from CompareContentOfPage:\n\n"

        '  IF the result contains "No changes" OR there are changes\n'
        "  but NO lines with REMOVED/ADDED/CHANGED:\n"
        "    → Go directly to STEP 6 (no email, no screenshot).\n\n"

        "  IF the result contains REMOVED or ADDED/CHANGED entries:\n"
        "    → Continue with STEP 4.\n\n"

        f"STEP 4: Try to take screenshot (OPTIONAL — do NOT skip Step 5 if this fails)\n"
        f'  4a) Call browser_navigate with url: "{url}"\n'
        "  4b) Wait until navigation is complete.\n"
        "  4c) Call browser_take_screenshot with exactly these parameters:\n"
        f'       {{ "fullPage": true, "filename": "{screenshot_filename}" }}\n'
        "  If the screenshot fails or shows an error page (e.g. Access Denied), that is OK.\n"
        "  ALWAYS proceed to Step 5 regardless of screenshot success or failure.\n\n"

        "STEP 5: Send email (MANDATORY when changes detected — do NOT skip this)\n"
        "  Summarize the changes briefly in English.\n"
        f"  Focus on changes relevant to: {name}.\n"
        + (f"  {prompt_context}\n" if prompt_context else "")
        + "  Call SendMailTo with exactly these parameters:\n"
        '    to: "<configured recipient>"\n'
        f'    subject: "🔔 Change Detected — {name}"\n'
        f"    body: Summary + specific changes (REMOVED/ADDED) + link {url}\n"
        f'    screenshot_filename: "{screenshot_filename}"  (omit this field if screenshot failed)\n\n'

        "STEP 6: Report result (ALWAYS execute)\n"
        "  Call ReportResult with:\n"
        "    changes_detected: true/false\n"
        "    removed: List of removed content (or empty list)\n"
        "    added: List of added content (or empty list)\n"
        f"    summary: Brief summary in English about changes on {name}\n"
        "    email_sent: true/false\n"

        "</workflow>\n\n"

        # --- IMPORTANT RULES ---
        "<rules>\n"
        "- Pure whitespace or formatting changes are NOT content changes.\n"
        "- Do NOT skip any step. Execute each step individually and sequentially.\n"
        "- ReportResult is ALWAYS called, even when no changes are found.\n"
        "- If screenshot fails, you MUST still send the email (Step 5). Just omit the screenshot_filename.\n"
        "- NEVER skip SendMailTo when changes are detected. The email is the primary deliverable.\n"
        "</rules>"
    )


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

def _make_event_handler(done: asyncio.Event):
    """Return a callback that logs streaming events and signals completion."""

    def _handler(event):
        etype = event.type.value if hasattr(event.type, "value") else str(event.type)

        if etype == "tool.execution_start":
            log.info("Tool started: %s", event.data.tool_name)
        elif etype == "tool.execution_complete":
            log.info("Tool complete: %s", event.data.tool_name)
        elif etype == "assistant.reasoning_delta":
            log.debug("Reasoning: %s", event.data.delta_content or "")
        elif etype == "session.idle":
            done.set()

    return _handler


# ---------------------------------------------------------------------------
# Run a single page
# ---------------------------------------------------------------------------

async def run_page(client: "CopilotClient", page: dict) -> None:
    """Run the monitoring agent for a single page configuration."""
    page_id = page["id"]
    name = page["name"]

    log.info("=" * 60)
    log.info("  Checking: %s", name)
    log.info("  URL: %s", page["url"])
    log.info("  Snapshot: snapshots/%s.txt", page_id)
    log.info("=" * 60)

    compare_tool = make_compare_tool(page_id)
    sendmail_tool = make_sendmail_tool(page_id)

    session = await client.create_session({
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "streaming": True,
        "on_permission_request": PermissionHandler.approve_all,
        "tools": [compare_tool, sendmail_tool, ReportResult],
        "mcp_servers": {
            "playwright": {
                "type": "local",
                "command": "npx",
                "args": [
                    "-y", "@playwright/mcp@latest",
                    "--headless",
                    "--output-dir", str(SCREENSHOT_DIR.resolve()),
                ],
                "tools": ["browser_navigate", "browser_take_screenshot"],
            },
        },
    })

    done = asyncio.Event()
    session.on(_make_event_handler(done))
    await session.send({"prompt": _build_prompt(page)})
    await done.wait()

    log.info("Finished: %s", name)
    log.info("")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    """Load pages and run the monitoring agent for each one sequentially."""
    # Optional --page filter
    target_page = None
    if "--page" in sys.argv:
        idx = sys.argv.index("--page")
        if idx + 1 < len(sys.argv):
            target_page = sys.argv[idx + 1]

    pages = load_pages()
    if not pages:
        log.error("No pages to monitor. Exiting.")
        return

    if target_page:
        pages = [p for p in pages if p["id"] == target_page]
        if not pages:
            log.error("Page '%s' not found in pages.json", target_page)
            return

    log.info("AI Scheduler — monitoring %d page(s)", len(pages))

    client_opts = {}
    cli_path = os.environ.get("COPILOT_CLI_PATH")
    if cli_path:
        client_opts["cli_path"] = cli_path
        log.info("Using external CLI: %s", cli_path)

    token = os.environ.get("COPILOT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        client_opts["github_token"] = token

    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    SCREENSHOT_DIR.mkdir(exist_ok=True)

    client = CopilotClient(client_opts)
    await client.start()

    try:
        for i, page in enumerate(pages, 1):
            log.info("[%d/%d] Starting page: %s", i, len(pages), page["name"])
            try:
                await run_page(client, page)
            except Exception:
                log.exception("Error processing page '%s'", page["name"])
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
