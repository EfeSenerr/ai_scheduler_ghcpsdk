"""
Page Change Checker

AI agent that monitors a configured URL for travel alert changes
and sends email notifications when differences are detected.

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
import logging
import os
import smtplib
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

URL = os.getenv("PAGE_URL", "https://www.qatarairways.com/en/travel-alerts.html")
SNAPSHOT_FILE = Path("previous_snapshot.txt")
SCREENSHOT_DIR = Path("screenshots")
MODEL = "gpt-5.4"
REASONING_EFFORT = "high"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


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
# Tools
# ---------------------------------------------------------------------------

class CompareParams(BaseModel):
    current_content: str = Field(description="The current text content of the page")


@define_tool(
    description=(
        f"Compares the page content against the stored snapshot "
        f"({SNAPSHOT_FILE}) and shows differences."
    )
)
async def CompareContentOfPage(params: CompareParams) -> str:  # noqa: N802
    """Compare the current page content against the stored snapshot."""
    log.info("Received content: %d characters", len(params.current_content))

    def _normalize(text: str) -> str:
        """Strip whitespace per line and collapse blank lines for stable hashing."""
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for i, line in enumerate(lines)
                         if line or (i > 0 and lines[i - 1]))

    if not SNAPSHOT_FILE.exists():
        log.warning("No snapshot found (%s)", SNAPSHOT_FILE)
        SNAPSHOT_FILE.write_text(params.current_content, encoding="utf-8")
        log.info("Initial snapshot saved (%d characters)", len(params.current_content))
        return (
            "No previous snapshot found. This is the first run. "
            "Snapshot has been saved."
        )

    previous_text = SNAPSHOT_FILE.read_text(encoding="utf-8")
    log.info("Snapshot loaded: %d characters", len(previous_text))

    current_hash = hashlib.sha256(_normalize(params.current_content).encode()).hexdigest()
    previous_hash = hashlib.sha256(_normalize(previous_text).encode()).hexdigest()
    log.info("Hash current:  %s…", current_hash[:16])
    log.info("Hash snapshot: %s…", previous_hash[:16])

    if current_hash == previous_hash:
        log.info("No changes.")
        return "No changes detected. The content is identical to the last snapshot."

    # Update snapshot immediately
    SNAPSHOT_FILE.write_text(params.current_content, encoding="utf-8")
    log.info("Changes detected! Snapshot updated.")

    # Generate human-readable diff
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

    # Always use NOTIFY_EMAIL env var for recipients (security: avoid leaking email via prompt/logs)
    notify_email = os.environ.get("NOTIFY_EMAIL", "")
    raw = notify_email if notify_email else params.to
    recipients = [addr.strip() for addr in raw.split(",") if addr.strip()]
    log.info("Sending email to %d recipients", len(recipients))
    log.info("Subject: %s", params.subject)

    # Check for screenshot attachment (Playwright saves to CWD or output-dir)
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
        # Multipart email with text + image attachment
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


# ---------------------------------------------------------------------------
# Agent prompt
# ---------------------------------------------------------------------------

def _build_prompt() -> str:
    """Build the agent instruction prompt, injecting runtime config."""
    notify_email = os.environ.get("NOTIFY_EMAIL", "")
    if not notify_email:
        log.warning("NOTIFY_EMAIL is not set – the agent cannot send emails.")

    return (
        # --- ROLE ---
        "<role>\n"
        "You are a deterministic monitoring agent. You execute exactly the steps described below, "
        "without deviation, without improvisation.\n\n"
        "CONTEXT: The user has a flight on 01.04.2026 from Munich (MUC) via Qatar to Oman. "
        "You are monitoring the Qatar Airways travel alerts page for any changes that could affect:\n"
        "  - Whether the user can fly from MUC on 01.04.2026\n"
        "  - Route disruptions or cancellations involving MUC, Doha (DOH), or Oman (MCT)\n"
        "  - Cancellation and rebooking policies that apply to this itinerary\n"
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
        f"  Call web_fetch with the URL: {URL}\n"
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

        f"STEP 4: Take screenshot\n"
        f"  4a) Call browser_navigate with url: \"{URL}\"\n"
        "  4b) Wait until navigation is complete.\n"
        "  4c) Call browser_take_screenshot with exactly these parameters:\n"
        '       {{ "fullPage": true, "filename": "pageToCheck.png" }}\n'
        "  Only proceed to Step 5 after a successful screenshot.\n\n"

        "STEP 5: Send email\n"
        "  Summarize the changes briefly in English.\n"
        "  Focus on: travel alerts, flight disruptions, cancellations, rebooking policies, "
        "and anything affecting the MUC → DOH → MCT route on 01.04.2026.\n"
        "  Specifically assess:\n"
        "    - Can the user still fly from MUC on 01.04.2026?\n"
        "    - Are there any cancellation or rebooking options mentioned?\n"
        "  Call SendMailTo with exactly these parameters:\n"
        '    to: "<configured recipient>"\n'
        '    subject: "🔔 Qatar Airways Travel Alert Change Detected – MUC 01.04.2026"\n'
        f"    body: Summary + specific changes (REMOVED/ADDED) + assessment for MUC flight on 01.04.2026 + cancellation info + link {URL}\n"
        '    screenshot_filename: "pageToCheck.png"\n\n'

        "STEP 6: Report result (ALWAYS execute)\n"
        "  Call ReportResult with:\n"
        "    changes_detected: true/false\n"
        "    removed: List of removed content (or empty list)\n"
        "    added: List of added content (or empty list)\n"
        "    summary: Brief summary in English, including impact on MUC → DOH → MCT flight on 01.04.2026\n"
        "    email_sent: true/false\n"

        "</workflow>\n\n"

        # --- IMPORTANT RULES ---
        "<rules>\n"
        "- Pure whitespace or formatting changes are NOT content changes.\n"
        "- Do NOT skip any step. Execute each step individually and sequentially.\n"
        "- ReportResult is ALWAYS called, even when no changes are found.\n"
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
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    """Start the Copilot agent, send the monitoring prompt, and wait."""
    client_opts = {}
    cli_path = os.environ.get("COPILOT_CLI_PATH")
    if cli_path:
        client_opts["cli_path"] = cli_path
        log.info("Using external CLI: %s", cli_path)

    # Use COPILOT_TOKEN (local .env) or GITHUB_TOKEN (Actions) for auth
    token = os.environ.get("COPILOT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        client_opts["github_token"] = token

    SCREENSHOT_DIR.mkdir(exist_ok=True)

    client = CopilotClient(client_opts)
    await client.start()

    try:
        session = await client.create_session({
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "streaming": True,
            "on_permission_request": PermissionHandler.approve_all,
            "tools": [CompareContentOfPage, SendMailTo, ReportResult],
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
        await session.send({"prompt": _build_prompt()})
        await done.wait()
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
