#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.27.0",
# ]
# ///

import argparse
import json
import os
import re
import sys
from typing import Any

from feishu_utils import get_tenant_access_token, request_json


def detect_format(content: str) -> str:
    """Auto-detect whether content should be sent as card or text.

    Returns 'card' if content contains markdown-like syntax or is multi-line (>3 lines).
    Returns 'text' otherwise.
    """
    lines = content.strip().split("\n")
    if len(lines) > 3:
        return "card"

    markdown_patterns = [
        r"```",          # code blocks
        r"\*\*.+\*\*",  # bold
        r"^#{1,3}\s",   # headings
        r"^[-*]\s",     # unordered list items
        r"^\d+\.\s",    # ordered list items
    ]
    for pattern in markdown_patterns:
        if re.search(pattern, content, re.MULTILINE):
            return "card"

    return "text"


def send_text_message(
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    token = get_tenant_access_token(app_id, app_secret)
    content = json.dumps({"text": text}, ensure_ascii=False)
    if reply_to_message_id:
        return request_json(
            "POST",
            f"/im/v1/messages/{reply_to_message_id}/reply",
            token=token,
            payload={"content": content, "msg_type": "text", "reply_in_thread": False},
        )
    return request_json(
        "POST",
        "/im/v1/messages",
        token=token,
        params={"receive_id_type": "chat_id"},
        payload={"receive_id": chat_id, "msg_type": "text", "content": content},
    )


def send_card_message(
    app_id: str,
    app_secret: str,
    chat_id: str,
    title: str,
    content: str,
) -> dict[str, Any]:
    token = get_tenant_access_token(app_id, app_secret)
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title}},
        "body": {
            "elements": [{"tag": "markdown", "content": content}]
        },
    }
    return request_json(
        "POST",
        "/im/v1/messages",
        token=token,
        params={"receive_id_type": "chat_id"},
        payload={
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        },
    )


def send_message(
    app_id: str,
    app_secret: str,
    chat_id: str,
    content: str,
    *,
    message_format: str,
    title: str | None = None,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    if message_format == "card":
        return send_card_message(
            app_id, app_secret, chat_id, title or "Bub", content
        )
    return send_text_message(
        app_id,
        app_secret,
        chat_id,
        content,
        reply_to_message_id=reply_to_message_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Feishu text or card message")
    parser.add_argument("--chat-id", "-c", required=True, help="Target chat ID")
    parser.add_argument("--content", "-m", required=True, help="Content to send (use '-' for stdin)")
    parser.add_argument(
        "--format",
        choices=("text", "card", "auto"),
        default="text",
        help="Message format: text, card, or auto (auto-detect based on content)",
    )
    parser.add_argument("--title", "-t", help="Card title when --format card is used")
    parser.add_argument(
        "--reply-to", "-r", help="Message ID to reply to for text messages"
    )
    parser.add_argument("--app-id", default=os.environ.get("BUB_FEISHU_APP_ID"))
    parser.add_argument("--app-secret", default=os.environ.get("BUB_FEISHU_APP_SECRET"))
    args = parser.parse_args()

    if not args.app_id or not args.app_secret:
        print("Error: BUB_FEISHU_APP_ID and BUB_FEISHU_APP_SECRET are required")
        sys.exit(1)

    # Read content from stdin or argument
    from_stdin = args.content == "-"
    content = sys.stdin.read() if from_stdin else args.content

    # Replace literal \n with real newlines when NOT reading from stdin
    if not from_stdin:
        content = content.replace("\\n", "\n")

    # Resolve auto format
    message_format = args.format
    if message_format == "auto":
        message_format = detect_format(content)

    if message_format == "card" and args.reply_to:
        print("Error: --reply-to is only supported when --format text is used")
        sys.exit(1)

    result = send_message(
        args.app_id,
        args.app_secret,
        args.chat_id,
        content,
        message_format=message_format,
        title=args.title,
        reply_to_message_id=args.reply_to,
    )
    if result.get("code") != 0:
        print(f"Error: {result.get('msg')}")
        sys.exit(1)
    print(f"{message_format.capitalize()} message sent to {args.chat_id}")


if __name__ == "__main__":
    main()