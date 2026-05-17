import asyncio
import logging
import os
import traceback
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from turnitin_automation import submit_and_get_report, test_login

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}
SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".doc")

UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

WELCOME_MESSAGE = """👋 *Welcome to the Turnitin Similarity Checker Bot\!*

I automatically upload your documents to Turnitin and send back the similarity report — no manual steps needed\.

━━━━━━━━━━━━━━━━━━━━━
📄 *Supported File Types*
• PDF \(`.pdf`\)
• Word Document \(`.docx`\)

━━━━━━━━━━━━━━━━━━━━━
⚙️ *How It Works*

1️⃣ *Upload your file* — send a PDF or DOCX directly in this chat
2️⃣ *Automatic submission* — I log into Turnitin and upload it to the assignment on your behalf
3️⃣ *Wait for processing* — Turnitin generates the similarity report \(usually 5–30 minutes\)
4️⃣ *Receive your report* — I send the similarity report PDF back to you automatically

━━━━━━━━━━━━━━━━━━━━━
⚠️ *Important Notes*

• 📁 Upload *one file at a time* — wait for the report before sending another
• 📏 Maximum file size: *20 MB*
• ⏳ Processing may take *longer during busy periods* \(evenings, deadlines\)
• 🔒 Your file is submitted securely and deleted from the server after processing

━━━━━━━━━━━━━━━━━━━━━
✅ *Ready\!* Just send me your PDF or DOCX file to get started\."""


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info(
        "Received /start from user_id=%s username=%s",
        user.id,
        user.username,
    )
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode="MarkdownV2")
    logger.info("Welcome message sent to user_id=%s", user.id)


async def _send_error_details(update: Update, result: dict) -> None:
    """Send full error details back to the Telegram user."""
    message = result.get("message", "Unknown error")
    page_url = result.get("page_url")
    tb_text = result.get("traceback", "")
    screenshot_path = result.get("screenshot_path")
    login_failed = result.get("login_failed", False)

    # ── Main error message ─────────────────────────────────────────────────
    if login_failed:
        error_text = (
            "❌ *Turnitin login failed*\n\n"
            "The bot could not authenticate with Turnitin\\. "
            "Check your credentials or whether Turnitin is blocking the session\\."
        )
        if page_url:
            error_text += f"\n\n*Page URL at failure:*\n`{page_url}`"
        await update.message.reply_text(error_text, parse_mode="MarkdownV2")
    else:
        error_text = (
            f"❌ *Turnitin automation failed*\n\n"
            f"*Error:* `{message}`"
        )
        if page_url:
            error_text += f"\n\n*Page URL when error occurred:*\n{page_url}"
        await update.message.reply_text(error_text, parse_mode="Markdown")

    # ── Traceback as a text file (skip for clean login failures) ──────────
    if tb_text:
        await update.message.reply_document(
            document=tb_text.encode(),
            filename="traceback.txt",
            caption="📋 Full traceback",
        )

    # ── Selenium screenshot ────────────────────────────────────────────────
    if screenshot_path:
        screenshot = Path(screenshot_path)
        if screenshot.exists():
            with open(screenshot, "rb") as f:
                caption = (
                    "🖥️ Browser screenshot immediately after login attempt"
                    if login_failed
                    else "🖥️ Browser screenshot at time of failure"
                )
                await update.message.reply_photo(photo=f, caption=caption)

    # ── Login debug HTML ───────────────────────────────────────────────────
    login_debug_html_path = result.get("login_debug_html_path")
    if login_debug_html_path:
        html_file = Path(login_debug_html_path)
        if html_file.exists():
            with open(html_file, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="login_debug.html",
                    caption="🔍 Full page HTML at time of login failure",
                )

    # ── Dashboard links file ───────────────────────────────────────────────
    dashboard_links_path = result.get("dashboard_links_path")
    if dashboard_links_path:
        links_file = Path(dashboard_links_path)
        if links_file.exists():
            with open(links_file, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="dashboard_links.txt",
                    caption="🔗 All links visible on the Turnitin dashboard at time of failure",
                )

    # ── Assignment links file ──────────────────────────────────────────────
    assignment_links_path = result.get("assignment_links_path")
    if assignment_links_path:
        al_file = Path(assignment_links_path)
        if al_file.exists():
            with open(al_file, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="assignment_links.txt",
                    caption="📋 All links/buttons on the class page at time of failure",
                )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    document = update.message.document
    mime_type = document.mime_type or ""
    file_name = document.file_name or "uploaded_file"
    file_size_mb = round(document.file_size / (1024 * 1024), 2) if document.file_size else 0

    logger.info(
        "Received document from user_id=%s: name=%s mime=%s size=%sMB",
        user.id,
        file_name,
        mime_type,
        file_size_mb,
    )

    # File size check (Telegram already blocks >20 MB, but guard anyway)
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            f"⚠️ File too large ({file_size_mb} MB). Maximum allowed size is 20 MB."
        )
        return

    is_supported = mime_type in SUPPORTED_MIME_TYPES or file_name.lower().endswith(
        SUPPORTED_EXTENSIONS
    )
    if not is_supported:
        await update.message.reply_text(
            "❌ Unsupported file type.\n\nPlease send a PDF or DOCX file.\n"
            "Use /start to see instructions."
        )
        return

    # ── Step 1: Save the file ──────────────────────────────────────────────
    await update.message.reply_text(
        f"✅ *File received:* `{file_name}`\n\nDownloading to server, please wait…",
        parse_mode="Markdown",
    )

    save_path = UPLOADS_DIR / file_name
    tg_file = await context.bot.get_file(document.file_id)
    await tg_file.download_to_drive(str(save_path))
    logger.info("File saved to %s", save_path)

    # ── Step 2: Run Turnitin workflow ──────────────────────────────────────
    await update.message.reply_text(
        "🔐 Logging into Turnitin and uploading your file…\n\n"
        "⏳ The similarity report usually takes *5–30 minutes* to generate\. "
        "I will send it to you automatically when it is ready\.",
        parse_mode="MarkdownV2",
    )
    logger.info("Starting Turnitin workflow for file=%s user_id=%s", file_name, user.id)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, submit_and_get_report, str(save_path))

    logger.info(
        "Turnitin workflow finished for user_id=%s: success=%s message=%s",
        user.id,
        result["success"],
        result["message"],
    )

    # ── Step 3: Send back the report ──────────────────────────────────────
    if result["success"] and result["report_path"]:
        report_path = Path(result["report_path"])
        if report_path.exists():
            await update.message.reply_text("📄 Similarity report is ready — sending now…")
            with open(report_path, "rb") as f:
                if report_path.suffix == ".pdf":
                    await update.message.reply_document(
                        document=f,
                        filename=report_path.name,
                        caption="✅ Here is your Turnitin similarity report.",
                    )
                else:
                    await update.message.reply_photo(
                        photo=f,
                        caption="✅ Here is a screenshot of your Turnitin similarity report.",
                    )
            logger.info("Report sent to user_id=%s", user.id)
        else:
            await update.message.reply_text(
                "⚠️ Report was generated but could not be located on the server. "
                "Please try again or contact support."
            )
    else:
        await _send_error_details(update, result)


async def cmd_test_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /test_login — verifies Turnitin credentials without uploading a file."""
    user = update.effective_user
    logger.info("Received /test_login from user_id=%s", user.id)

    await update.message.reply_text(
        "🔐 Testing Turnitin login… this usually takes 10–20 seconds."
    )

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, test_login)

    success = result.get("success", False)
    message = result.get("message", "Unknown result")
    url = result.get("url", "")
    page_title = result.get("page_title", "")
    screenshot_path = result.get("screenshot_path")
    html_path = result.get("login_debug_html_path")

    logger.info(
        "test_login finished for user_id=%s: success=%s url=%s", user.id, success, url
    )

    class_found = result.get("class_found", False)
    assignment_found = result.get("assignment_found", False)
    class_url = result.get("class_url", "")
    assignment_url = result.get("assignment_url", "")

    if success and class_found and assignment_found:
        reply = (
            "✅ *All checks passed*\n\n"
            f"🔐 Login: ✓\n"
            f"🏫 Class *Political Science*: ✓\n"
            f"📝 Assignment *mohit sir assignment*: ✓\n\n"
            f"*Page title:* `{page_title}`\n"
            f"*URL:* `{url}`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
    elif success and class_found and not assignment_found:
        reply = (
            "⚠️ *Partial — assignment not found*\n\n"
            f"🔐 Login: ✓\n"
            f"🏫 Class *Political Science*: ✓\n"
            f"📝 Assignment *mohit sir assignment*: ✗ not found in class\n\n"
            f"*Class URL:* `{class_url}`\n"
            f"*Current URL:* `{url}`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
    elif success and not class_found:
        reply = (
            "⚠️ *Partial — class not found*\n\n"
            f"🔐 Login: ✓\n"
            f"🏫 Class *Political Science*: ✗ not visible on dashboard\n"
            f"📝 Assignment *mohit sir assignment*: ✗ not checked\n\n"
            f"*Page title:* `{page_title}`\n"
            f"*URL:* `{url}`"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
    else:
        reply = (
            "❌ *Turnitin login failed*\n\n"
            f"*Reason:* `{message}`\n"
        )
        if url:
            reply += f"\n*URL after submit:* `{url}`"
        if page_title:
            reply += f"\n*Page title:* `{page_title}`"
        await update.message.reply_text(reply, parse_mode="Markdown")

    # Always send screenshot
    if screenshot_path:
        ss = Path(screenshot_path)
        if ss.exists():
            with open(ss, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption="🖥️ Browser screenshot after login attempt",
                )

    # Send HTML only on failure
    if not success and html_path:
        hf = Path(html_path)
        if hf.exists():
            with open(hf, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="login_debug.html",
                    caption="🔍 Full page HTML for debugging",
                )


async def log_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all handler that logs every incoming update."""
    user = update.effective_user
    msg = update.message
    if msg:
        logger.info(
            "Incoming message from user_id=%s username=%s: text=%r has_document=%s",
            user.id if user else "unknown",
            user.username if user else "unknown",
            msg.text,
            bool(msg.document),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled exceptions raised inside handlers."""
    logger.error(
        "Unhandled exception while processing update:\n%s",
        "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__)),
    )
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(
            "⚠️ An unexpected error occurred. Please try again in a moment."
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    app = ApplicationBuilder().token(token).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test_login", cmd_test_login))

    # Document handler
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Catch-all logger for every other message type (text, photos, etc.)
    app.add_handler(MessageHandler(filters.ALL, log_all_messages))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Bot is running — polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
