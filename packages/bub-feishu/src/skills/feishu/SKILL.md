---
name: feishu
description: |
  Use this skill to respond to messages from feishu channel.
---

# Feishu Skill

Agent-facing execution guide for outbound communication in Feishu/Lark.

Assumption: `BUB_FEISHU_APP_ID` and `BUB_FEISHU_APP_SECRET` are already available.

## Required Inputs

- `chat_id`: required for sending messages (from current channel/session context)
- `message_id`: required for reply, edit, and reaction actions
- `content` / `text`: required for send or edit

## Format Selection Rules

```
if content has markdown (```, **, lists, code) OR content > 3 lines:
    use --format card
elif content is short, plain, conversational:
    use --format text
else:
    use --format auto   (script auto-detects)
```

- `--format auto`: auto-detects based on content. Uses card if markdown patterns or >3 lines detected; text otherwise.
- `--format text`: plain text message. Supports `--reply-to`.
- `--format card`: interactive card with lark_md body. Does NOT support `--reply-to`.

Fallback: if card delivery fails, retry with `--format text`.

## Lark Markdown Supported Syntax (in cards)

Supported in `lark_md` (card elements):

| Syntax | Example |
|--------|---------|
| Newline | `\n` |
| Bold | `**text**` |
| Italic | `*text*` |
| Strikethrough | `~~text~~` |
| Links | `[text](url)` |
| Ordered lists | `1. item` (Feishu 7.6+) |
| Unordered lists | `- item` (Feishu 7.6+) |
| Code blocks | ` ```code``` ` (Feishu 7.6+) |
| Images | `![alt](image_key)` |
| Divider | `\n ---\n` |
| @mention | `<at id=open_id></at>` |
| Colored text | `<font color='red'>text</font>` |

NOT supported in `lark_md`:

- Headings (`#`, `##`, `###`) → use `**bold**` instead
- Blockquotes (`>`)
- Tables

Note: text element `lark_md` mode (non-card) is more limited — no images, dividers, lists, or code blocks.

## Execution Policy

1. If `message_id` is known, prefer reply semantics.
2. If `sender_is_bot=true`, prefer a normal message (not reply).
3. For long-running tasks: send acknowledgment → progress (edit) → completion.
4. When only lightweight acknowledgment is needed, use a reaction instead of a message.
5. When blocked or failing, send a problem report immediately.
6. **Always pass content via stdin (heredoc).** Never embed multi-line text in shell arguments.
7. Literal `\n` in argument mode is auto-converted to real newlines. Stdin mode passes content as-is.

## Command Templates

All paths use `${SKILL_DIR}` which resolves to this skill's directory.

### Send message (stdin — preferred)

```bash
# Auto-detect format
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --content - --format auto
Build finished successfully.
Summary:
- 12 tests passed
- 0 failures
EOF

# Explicit text format
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --content - --format text
Short reply here.
EOF

# Card with title
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --content - --format card --title "Deploy Status"
**Status:** Complete
- Service A: ✅
- Service B: ✅
EOF
```

### Reply to a message

```bash
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --content - --format text --reply-to <MESSAGE_ID>
Got it, working on it now.
EOF
```

### Edit an existing message

```bash
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/feishu_edit.py --message-id <MESSAGE_ID> --text -
Updated content here.
EOF
```

### Short inline (simple cases only)

```bash
# Literal \n is converted to real newlines automatically in argument mode
uv run ${SKILL_DIR}/scripts/feishu_send.py --chat-id <CHAT_ID> --content "Line 1\nLine 2" --format text
```

## Sending Images via `lark-cli`

`feishu_send.py` does not handle images. Use `lark-cli` instead:

```bash
# Send image to chat
lark-cli im +messages-send --chat-id <CHAT_ID> --image ./photo.png --as bot

# Reply with image
lark-cli im +messages-reply --message-id <MESSAGE_ID> --image ./photo.png --as bot

# Send by image_key
lark-cli im +messages-send --chat-id <CHAT_ID> --image img_v3_abc123 --as bot
```

Do not put local file paths in Markdown and expect upload. Use `--image` flag.

## Script Interface Reference

### `feishu_send.py`

| Arg | Required | Notes |
|-----|----------|-------|
| `--chat-id`, `-c` | yes | Target chat |
| `--content`, `-m` | yes | Use `-` for stdin |
| `--format` | no | `text` (default), `card`, or `auto` |
| `--title`, `-t` | no | Card title (only with `--format card`) |
| `--reply-to`, `-r` | no | Only with `--format text` |
| `--app-id` | no | Env fallback: `BUB_FEISHU_APP_ID` |
| `--app-secret` | no | Env fallback: `BUB_FEISHU_APP_SECRET` |

### `feishu_edit.py`

| Arg | Required | Notes |
|-----|----------|-------|
| `--message-id`, `-m` | yes | Message to edit |
| `--text`, `-t` | yes | Use `-` for stdin |
| `--app-id` | no | Env fallback: `BUB_FEISHU_APP_ID` |
| `--app-secret` | no | Env fallback: `BUB_FEISHU_APP_SECRET` |

## Failure Handling

- Card fails → fall back to `--format text`
- Reply fails → fall back to normal send to same `chat_id`
- Edit fails → send new message stating it's the updated result
- Reaction fails → send short text acknowledgment
- Missing `message_id` → skip reply/edit/reaction
- Missing `chat_id` → skip send

## API Reference

- Feishu: `https://open.feishu.cn/document/`
- Lark: `https://open.larksuite.com/document/`
- Key endpoints:
  - `POST /open-apis/im/v1/messages` (send)
  - `POST /open-apis/im/v1/messages/{message_id}/reply` (reply)
  - `PATCH /open-apis/im/v1/messages/{message_id}` (edit)
  - `POST /open-apis/im/v1/messages/{message_id}/reactions` (react)
