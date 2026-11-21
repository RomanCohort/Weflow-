# WeChat Oracle

Local-first WeChat group archive and agent assistant.

[简体中文](README.zh-CN.md)

WeChat Oracle records WeChat group messages through [WeFlow](https://github.com/hicccc77/WeFlow), stores them in SQLite, optionally OCRs images and transcribes voice messages locally, and lets an LLM-backed bot answer questions inside the group. The bot can also maintain long-term per-group memory in the background.

The project is designed for a personal Windows machine running WeChat PC. Message data, media, memory, and debug logs stay under `data/` unless you explicitly send content to an LLM or vision provider.

## What It Does

- Ingests live WeChat messages from WeFlow SSE into SQLite.
- Imports historical WeFlow JSON exports.
- Stores normalized messages, forwarded-message children, media paths, OCR/ASR transcripts, command runs, agent memory, and audit traces.
- Answers in group chat through wx4py UI automation.
- Supports slash commands such as `/find`, `/sum`, `/recent`, `/ask`, `/explain`, and `/balance`.
- Supports free-form agent chat when directly mentioned, replied to, or probability-triggered.
- Supports Local Ask from the terminal UI or CLI: ask the bot about one selected group without sending anything to WeChat.
- Runs a silent `lurk` learning path that updates `group_memory` / `persona_drift` without sending group messages.
- Can use either the native in-process tool loop or an OpenClaw-backed runtime for agent turns.

## Architecture

```text
WeChat PC
   |
   v
WeFlow HTTP API
   |                         optional local worker
   | SSE live messages       OCR / ASR
   v                         |
ingest live -----------------+
   |
   v
SQLite WAL database: data/wechat-oracle.db
   |
   +--> dispatcher --> LLM / agent backend --> wx4py reply to WeChat
   |
   +--> local ask  --> LLM / agent backend --> terminal UI only
   |
   +--> agent lurk --> memory writes only, no reply
```

Production can be started with one supervisor command:

```powershell
uv run wechat-oracle run
```

`run` starts `ingest live` and `dispatcher` together. `ingest live` starts the SSE subscriber and an embedded mm worker thread. `dispatcher` polls SQLite, processes explicit commands and agent triggers with per-group ordering and cross-group parallelism, and serializes all wx4py sends through one sender thread so only one GUI operation touches WeChat at a time.

By default `run` opens a Textual terminal UI: the top area stays fixed with bot configuration, selected Local Ask group, and process status, while the rest of the screen scrolls live logs from both child processes. Press `a` to open a one-shot Local Ask dialog, or `c` to edit the agent backend/model, probability wakeup, and reply mention policy. Saving the config writes `.env` and restarts the dispatcher so group replies use the new settings. Use `uv run wechat-oracle run --plain` to disable the TUI and let child processes write directly to the terminal.

## Requirements

- Windows 10/11.
- WeChat PC 4.1.8.107, Qt version, is the recommended runtime for wx4py replies. Newer 4.1.10.x clients may break UI control.
- [WeFlow desktop](https://github.com/hicccc77/WeFlow) with HTTP API enabled. This project does not install WeFlow for you; install and start it first, then copy the HTTP API access token into `WO_WEFLOW_TOKEN`.
- Python 3.12+.
- [uv](https://docs.astral.sh/uv/).
- An OpenAI-compatible LLM endpoint, or OpenClaw local gateway for `WO_AGENT_BACKEND=openclaw`.

Non-reply workflows such as DB initialization, historical import, status checks, and WeFlow diagnostics can run without wx4py. Sending replies requires WeChat's main window to be visible, not minimized to tray.

## Quick Start

```powershell
git clone https://github.com/<your-account>/WeChat-Oracle.git
cd WeChat-Oracle
uv sync
uv run wechat-oracle setup
uv run wechat-oracle doctor
uv run wechat-oracle run
```

`setup` writes a minimal `.env`, `doctor` checks the database, WeFlow, the selected agent backend, and the reply path, and `run` starts live ingest plus dispatcher with a small terminal UI. On Windows you can also double-click `scripts\run.bat` after setup.

Before running `setup`, install and start WeFlow, open its settings, enable the HTTP API service, and copy the access token when prompted.

For manual setup, create `.env` in the repository root. Start with the common settings:

```env
# WeFlow
WO_WEFLOW_TOKEN=<weflow-token>
WO_GROUPS=

# Bot identity in the target group
WO_BOT_NAME=<bot-group-nickname>
# Optional. Set manually if quote-replying to the bot does not trigger.
# WO_BOT_WXID=wxid_xxx
# Optional verified account the bot may address as father. Read from the
# trigger row's sender_wxid; never from a display name or message text.
# WO_FATHER_WXID=wxid_xxx

# Reply path
WO_REPLY=True
WO_REPLY_BACKEND=wx4py
WO_REPLY_MENTION_POLICY=explicit

# Optional agent tuning
WO_AGENT_BASE_PROBABILITY=0.25
WO_AGENT_PROACTIVE_MODE=reactive
WO_AGENT_PROACTIVE_GROUPS=
WO_AGENT_CONTINUATION_ENABLED=True
WO_AGENT_CONTINUATION_MAX_FOLLOWUPS=2
WO_AGENT_CONTINUATION_DELAY_SECONDS=90
WO_AGENT_CONTINUATION_TTL_SECONDS=600
WO_AGENT_RECENT_CONTEXT_CHAT=100
WO_LLM_MAX_TOKENS=5000
WO_LLM_WRITE_MAX_TOKENS=10000

# Optional image reading
WO_VISION_API_KEY=
WO_VISION_ENDPOINT=https://dashscope.aliyuncs.com/compatible-mode/v1
WO_VISION_MODEL=qwen-vl-plus
WO_VISION_MAX_IMAGES=3
WO_VISION_MAX_TOKENS=800

# Optional background learning
WO_AGENT_LURK_ENABLED=False
WO_AGENT_LURK_INTERVAL_SECONDS=1800
WO_AGENT_LURK_MIN_NEW_MESSAGES=20
```

`WO_BOT_NAME` and `WO_BOT_WXID` identify different things. `WO_BOT_NAME` is the nickname used to detect real `@<bot>` mentions in the group. `WO_BOT_WXID` is the bot account's own wxid and is only needed for the reply-to-bot trigger. The dispatcher auto-discovers it from stored bot messages when possible; set it manually if quote-replying to the bot does not wake the agent.

`WO_FATHER_WXID` optionally binds one verified WeChat account to the address "father". Only a direct mention or reply whose stored `sender_wxid` matches receives the address rule. Nicknames, quoted text, chat history, and claims inside a message are never accepted as identity. The address affects tone only; tool permissions, class-officer authorization, confirmation flows, and factual rules remain unchanged.

Then choose one agent backend.

Option A: use an ordinary OpenAI-compatible API directly:

```env
WO_AGENT_BACKEND=native
WO_LLM_API_KEY=<api-key>
WO_LLM_ENDPOINT=https://api.deepseek.com
WO_LLM_MODEL=deepseek-v4-pro
```

Option B: use OpenClaw as the agent runtime:

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
WO_OPENCLAW_TIMEOUT_SECONDS=300
```

Then either run both processes through the supervisor:

```powershell
uv run wechat-oracle run
```

Or run them manually in two terminals:

```powershell
# Terminal 1: live ingest + OCR/ASR worker
uv run wechat-oracle ingest live

# Terminal 2: command/agent dispatcher
uv run wechat-oracle dispatcher
```

In a watched group:

```text
@<bot-name> /help
@<bot-name> Who mentioned stocks today?
@<bot-name> /find since:2026-05 about home renovation
```

If `WO_GROUPS` is empty, live ingest monitors every group chat currently exposed by WeFlow sessions. You can also set `WO_GROUPS` to a comma-separated list of group names, remarks, or wxids.

## Core Commands

General CLI:

```powershell
uv run wechat-oracle setup
uv run wechat-oracle doctor
uv run wechat-oracle run
uv run wechat-oracle init-db
uv run wechat-oracle status
uv run wechat-oracle ingest live
uv run wechat-oracle ingest backfill <export.json> --format weflow
uv run wechat-oracle dispatcher
uv run wechat-oracle worker mm
```

WeFlow diagnostics:

```powershell
uv run wechat-oracle weflow find <group-name-or-wxid-fragment>
uv run wechat-oracle weflow sessions --groups-only
```

Agent state:

```powershell
uv run wechat-oracle agent show <group_id>
uv run wechat-oracle agent show-runs <group_id> -n 10
uv run wechat-oracle agent ask <group_id-or-name> summarize today
uv run wechat-oracle agent ask <group_id-or-name> update memory from this topic --write
uv run wechat-oracle agent lurk <group_id>
uv run wechat-oracle agent wipe <group_id>
uv run wechat-oracle agent wipe <group_id> --persona-only
uv run wechat-oracle agent wipe <group_id> --memory-only -y
```

Health checks:

```powershell
uv run wechat-oracle verify roundtrip
```

OpenClaw runtime helpers:

```powershell
uv run wechat-oracle openclaw ping
uv run wechat-oracle openclaw mcp-test
uv run wechat-oracle openclaw mcp-serve
```

`openclaw mcp-serve` is a stdio MCP server entrypoint intended to be spawned by OpenClaw. `openclaw ping` tests the configured OpenClaw chat-completions gateway.

Convenience wrappers:

```powershell
scripts\run.bat
scripts\track.bat
scripts\import.bat <export.json>
```

`scripts\run.bat` starts `wechat-oracle run`. `scripts\track.bat` is the lower-level live-ingest-only wrapper kept for debugging.

## Historical Backfill

Backfill imports historical WeFlow exports into the same SQLite archive used by live ingest:

```powershell
uv run wechat-oracle ingest backfill <export.json> --format weflow
uv run wechat-oracle ingest backfill <weflow-export-dir> --format weflow
```

The command imports one export file, or all JSON files under a WeFlow export directory's `texts/` folder. Re-running the same file or directory is safe: all imported messages go through `ingest/writer.py:write_messages()`, and `UNIQUE(dedupe_key)` skips duplicates.

Supported formats:

| Format | Input |
|---|---|
| `weflow` | WeFlow JSON export with top-level `session` and `messages` fields, or a WeFlow export directory containing `texts/`. |
| `jsonl` | One canonical `Message` JSON object per line, mainly useful for pipeline tests or custom importers. |

For WeFlow exports, the importer:

- Derives `group_id` from `session.wxid`, falling back to the filename stem.
- Derives `group_name` from `session.displayName`, `session.nickname`, or `session.remark`.
- Converts WeFlow `localType` values into normalized message types: text, image, voice, video, sticker, link, quote, forward, and system.
- Preserves quote replies using `reply_to_wx_msg_id` and `quote_text`.
- Parses merged-forward messages into `forwarded_records`, so `/find`, `/sum`, and agent history tools can search inside forwarded bundles. Forwarded child media gets its own `media_path` when WeFlow/export XML exposes a local file, or when `src_msg_id` matches an already archived media message.
- Copies referenced local media files from the export folder into `data/media/<group_id>/<kind>/` and stores a `data/`-relative `media_path`.

If a media file referenced by the export is missing, the message is still imported with a missing-media placeholder. OCR/ASR can only run for media files that exist under `data/media`.

Backfilled rows use `source='backfill'`. The dispatcher only wakes on new `source='live'` rows, so importing old messages will not make the bot reply to historical mentions. Backfilled messages are still available to `/find`, `/sum`, `/recent`, agent history search, and manual or automatic `lurk` learning.

## In-Group Commands

The dispatcher accepts explicit slash commands either as `@<bot> /cmd ...` or as a standalone `/cmd ...` group message.

| Command | Purpose |
|---|---|
| `/find [from:<person>\|@<person>] [since:YYYY[-MM[-DD]]] <query>` | Semantic search over the current group archive. |
| `/sum [from:<person>\|@<person>] [since:YYYY[-MM[-DD]]] [limit:N] [topic]` | Summarize current-group messages. |
| `/recent [N]` | Show recent ingested messages without calling an LLM. |
| `/ask <question>` | Lightweight LLM call with no group-history context. |
| `/explain [text]` | Explain quoted message or supplied text. |
| `/balance` | Query the configured DeepSeek-compatible balance endpoint. |
| `/help [command]` | Show command help. |
| `/kb find/show/versions/event/add/revise/link <参数>` | 本群资料、来源、版本与事项关联；检索和阅读对所有人开放。 |
| `/today` / `/week` | 从数据库读取已登记事项，标注原始来源；不推测未登记通知。 |

Examples:

```text
@Assistant /find from:Alice since:2026-05 about home renovation
@Assistant /sum limit:100 what did people discuss last night?
@Assistant /recent 20
@Assistant /ask What is SQLite WAL?
Reply to an image and send: @Assistant /explain
```

## Agent Behavior

There are four separate runtime paths.

### 1. Collection Path

`ingest live`, `ingest backfill`, and `worker mm` only write normalized data to SQLite. They do not decide whether the bot should speak.

### 2. Interaction Path

The dispatcher classifies each new live message:

| Trigger | Condition | Behavior |
|---|---|---|
| `mention` | Text contains a real `@<WO_BOT_NAME>` mention. | Always wakes the bot. Slash commands are parsed; bare text enters agent chat. |
| `reply` | Message quote-replies to a prior bot message, and `WO_BOT_WXID` is known or auto-discovered. | Always wakes the bot. |
| `probability` | Not mention/reply, `WO_AGENT_PROACTIVE_MODE` is not `off`, and the message passes type gate, random threshold, and cooldown. | Agent decides whether to speak or `stay_silent`, following the configured participation posture. |
| none | No trigger. | No LLM call; message is finalized as no-trigger. |

Direct mention and reply-to-bot triggers run the agent directly. The bot sends a group reply only if the agent returns a non-empty response.

`WO_REPLY_MENTION_POLICY` controls whether outgoing group replies notify a member:

- `always`: every group reply attempts a real `@requester`.
- `explicit`: the default. Direct mentions, reply-to-bot turns, slash commands, and command errors `@` the requester; `probability` / `proactive` ambient replies do not.
- `never`: send plain group messages without `@`.

`WO_AGENT_PROACTIVE_MODE` controls only non-explicit probability wakeups:

- `off`: never wake from ambient chat; only direct mentions, reply-to-bot, and slash commands run the agent.
- `reactive`: the default. A probability wakeup may join the current topic only when it has useful context.
- `proactive`: a probability wakeup may also ask one short context-based question, connect an old thread, or start a light related topic when the timing is appropriate.

Continuation follow-ups are separate from probability. When `WO_AGENT_CONTINUATION_ENABLED=True`, the agent may call `schedule_followup` while replying. The system stores only an intent and reruns the agent later with fresh context:

- `committed`: the bot explicitly promised to come back, so the follow-up can run even if nobody speaks meanwhile.
- `thread`: the bot may continue only if new non-bot messages keep the same topic moving; otherwise the job is cancelled.

Follow-ups use the same per-group dispatcher queue and serialized wx4py sender as normal replies. They respect `WO_AGENT_CONTINUATION_MAX_FOLLOWUPS`, `WO_AGENT_CONTINUATION_DELAY_SECONDS`, `WO_AGENT_CONTINUATION_TTL_SECONDS`, and `WO_REPLY_MENTION_POLICY`.

The native agent has two phases:

- Phase A reads recent context, may call read-only tools, and returns either a chat reply or `stay_silent`.
- Phase B sees the Phase A trace and may update `group_memory` or `persona_drift`.

Phase A initial context includes the latest `WO_AGENT_RECENT_CONTEXT_CHAT` messages from the current group. Tools can search older history, expand quote chains, expand forwarded bundles, read OCR/ASR text, read images through a vision model, read voice transcripts, or read group memory.

### 3. Local Ask Path

The Textual UI and CLI can run an agent turn against a selected group without sending anything to WeChat.

```powershell
uv run wechat-oracle agent ask <group_id-or-name> summarize today's discussion
uv run wechat-oracle agent ask <group_id-or-name> summarize Alice in August 2024 and update memory --write
```

In the TUI, use keyboard shortcuts:

```text
g  open group picker
a  open Local Ask dialog for the selected group
m  edit group_memory / persona_drift for the selected group
c  edit agent backend / native model / OpenClaw agent id / probability wakeup / @ policy, then restart dispatcher
w  toggle read-only/write mode
```

Local Ask defaults to read-only: it may read recent context, search history, inspect media, and read group memory, but it will not update `group_memory` / `persona_drift`. Use `--write` in the CLI or press `w` in the TUI to run a `local_task` that may update memory. Local Ask writes an `agent_run_log` audit row with `trigger_kind='local_ask'` or `trigger_kind='local_task'`; it never writes `command_runs` and never calls wx4py.

### 4. Background Learning Path

`agent lurk <group_id>` and optional auto-lurk read a watermarked batch of new messages and update memory without replying.

- First run reads the latest `WO_AGENT_LURK_RECENT_MSGS` messages.
- Later runs continue from `agent_lurk_state.last_msg_id`.
- The lurk agent may call history tools to inspect older messages when the new batch points to prior context.
- It writes only stable, reusable knowledge to `group_memory` or long-term behavior adjustments to `persona_drift`.
- It never calls wx4py and never sends an acknowledgement.

Enable automatic lurk inside dispatcher with:

```env
WO_AGENT_LURK_ENABLED=True
WO_AGENT_LURK_INTERVAL_SECONDS=1800
WO_AGENT_LURK_MIN_NEW_MESSAGES=20
```

Auto-lurk uses a separate single worker, so it does not occupy chat response workers.

## Agent Backends

`WO_AGENT_BACKEND=native` is the default. It runs the tool-calling loop inside this process through your OpenAI-compatible LLM endpoint.

`WO_AGENT_BACKEND=openclaw` delegates chat and lurk turns to an OpenClaw agent through a local OpenAI-compatible gateway. OpenClaw can use the project's MCP server to call WeChat-Oracle tools. This is useful if your production bot should use an existing subscription-backed agent runtime.

Switching between the two modes is controlled by one setting:

```env
# Use the ordinary OpenAI-compatible API directly.
WO_AGENT_BACKEND=native

# Or use OpenClaw as the agent runtime.
WO_AGENT_BACKEND=openclaw
```

Important distinction: OpenClaw here is an agent runtime backend, not a WeChat reply backend. Group replies still go through wx4py or `stdout`.

OpenClaw-related settings:

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
WO_OPENCLAW_TIMEOUT_SECONDS=300
```

## Real WeChat Mentions

`Wx4pyReplier` attempts to create a real WeChat group mention token by opening the group, typing `@`, selecting the requester from WeChat's candidate popup, then pasting the reply body. If the candidate cannot be detected, it falls back to plain text `@name` and logs a warning.

This path is used only when `WO_REPLY_MENTION_POLICY` says the current reply should mention someone.

This is intentionally serialized through `_SerialReplier`; multiple worker threads never manipulate the WeChat GUI at the same time.

## Multimedia

The mm worker fills `messages.transcript`:

- Images and stickers: local OCR through `rapidocr-onnxruntime`.
- Voice: local ASR through `faster-whisper`.

Transcript states:

- `NULL`: not processed yet.
- `''`: processed but no text recognized.
- non-empty text: usable OCR/ASR result.

If `WO_VISION_API_KEY` is configured, agent chat and `/explain` / `/ask` can send referenced images to a vision model for direct reading. Without it, native image handling falls back to OCR text and placeholders. In OpenClaw mode, MCP also exposes `load_image`, which returns the original image block for a vision-capable OpenClaw agent to inspect directly; `read_image` still means "return a textual vision-model reading".

## Data Model

For the full ingestion contract, including WeFlow `localType` handling, app-message subtypes, media storage, quote/forward normalization, and LLM-visible rendering, see [docs/message-model.md](docs/message-model.md).

Key tables:

| Table | Purpose |
|---|---|
| `messages` | Normalized WeChat messages. |
| `forwarded_records` | Children inside merged-forward messages. |
| `command_runs` | Dispatcher idempotency and command status. |
| `group_state` | Live/backfill per-group cursors. |
| `persona_drift` | Per-group evolvable behavior supplement. |
| `group_memory` | Per-group freeform long-term memory document. |
| `agent_run_log` | Agent audit traces for chat, lurk, and Local Ask turns. |
| `agent_lurk_state` | Lurk cursor, separate from audit logs. |
| `agent_proactive_outbox` | Delayed proactive continuation jobs. Stores intent, not pre-generated reply text. |

All write paths for imported messages go through `ingest/writer.py:write_messages()` and rely on `UNIQUE(dedupe_key)` for cross-source deduplication.

`group_memory` and `persona_drift` are replace-on-write. Write tools force the agent to read current state before writing, and they check that state did not change in between. If another concurrent agent run updated memory first, the tool asks the model to re-read and merge.

## Configuration Reference

All runtime settings use the `WO_` prefix and can be set in `.env` or the process environment.

| Variable | Default | Notes |
|---|---:|---|
| `WO_DATA_DIR` | `data` | Data root. |
| `WO_DB_PATH` | `data/wechat-oracle.db` | SQLite database path. |
| `WO_MEDIA_DIR` | `data/media` | Media directory. |
| `WO_GROUPS` | `[]` | Empty means all WeFlow group sessions; comma string or JSON list is accepted. |
| `WO_EMBEDDINGS_ENABLED` | `false` | 启用已有本地 Qwen3 语义接口；关闭或故障时保留关键词检索。 |
| `WO_EMBEDDINGS_ENDPOINT` | `http://127.0.0.1:8092/v1` | 仅允许 loopback HTTP；不启动或修改共享模型服务。 |
| `WO_EMBEDDINGS_MODEL` | empty | 仅当接口只提供一个模型时自动识别，否则需明确配置。 |
| `WO_EMBEDDINGS_REVISION` | `qwen3-v1` | 同模型名更换权重时手动变更，后台重建向量，避免混用。 |
| `WO_EMBEDDINGS_TIMEOUT` | `30` | 单次本地接口超时秒数。 |
| `WO_RNAHARNESS_GROUPS` | `[]` | 按真实群ID启用科研意图的直接 @/回复；普通聊天仍走常规群聊 agent。JSON数组或逗号分隔，空列表关闭。 |
| `WO_RNAHARNESS_ENDPOINT` | `http://127.0.0.1:8787` | 已运行的研究 gateway，仅允许 loopback HTTP origin。 |
| `WO_RNAHARNESS_TIMEOUT_SECONDS` | `660` | 等待任务结果的上限秒数；不确定提交不会自动重提。 |
| `WO_RNA_MCP_URL` | AgentArts `rnaharness-mcp` endpoint | 短任务、医学证据和实体工具使用的签名 MCP 入口。 |
| `WO_RNA_HARNESS_ROOT` | `C:/RNAHarness` | 长任务使用的本机 RNAHarness worker 源码根目录。 |
| `WO_RNA_JOB_TIMEOUT_SECONDS` | `600` | 单个本地长任务的上限秒数；超时后不会自动重新提交。 |
| `WO_LOG_LEVEL` | `INFO` | loguru level. |
| `WO_WX4PY_LOG_LEVEL` | `WARNING` | Python logging level for wx4py internals; set to `INFO` only when debugging UI automation. |
| `WO_WEFLOW_BASE_URL` | `http://127.0.0.1:5031` | WeFlow HTTP API root. |
| `WO_WEFLOW_TOKEN` | empty | WeFlow token. |
| `WO_BOT_NAME` | empty | Bot's group nickname. Required by dispatcher. |
| `WO_BOT_WXID` | empty | Optional bot wxid for reply-to-bot trigger. |
| `WO_FATHER_WXID` | empty | Verified account the bot may address as father on direct mentions/replies; no permission changes. |
| `WO_LLM_PROVIDER` | `openai-compatible` | LLM provider adapter. |
| `WO_LLM_API_KEY` | empty | LLM API key. |
| `WO_LLM_ENDPOINT` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `WO_LLM_MODEL` | `deepseek-v4-pro` | Model name. |
| `WO_LLM_JSON_MODE` | `native` | `native` or `prompt` JSON mode. |
| `WO_DISPATCHER_POLL_INTERVAL` | `3.0` | DB polling interval in seconds. |
| `WO_DISPATCHER_WORKER_THREADS` | `4` | Global message workers. Messages are serialized per group but different groups can run in parallel; wx4py sends remain serialized. |
| `WO_DISPATCHER_CANDIDATE_LIMIT` | `500` | `/find` candidate cap. |
| `WO_DISPATCHER_CONTEXT_CHAT` | `2500` | Legacy chat context cap, still used by some summary paths. |
| `WO_LLM_MAX_TOKENS` | `5000` | General output cap. |
| `WO_LLM_CHAT_MAX_TOKENS` | empty | Overrides chat cap. |
| `WO_LLM_SUM_MAX_TOKENS` | empty | Overrides summary cap. |
| `WO_LLM_SHORT_MAX_TOKENS` | empty | Overrides `/ask` / `/explain` cap. |
| `WO_LLM_WRITE_MAX_TOKENS` | `10000` | Cap for memory-write tool calls. |
| `WO_VISION_PROVIDER` | `openai-compatible` | Vision adapter. |
| `WO_VISION_API_KEY` | empty | Enables direct image reading. |
| `WO_VISION_ENDPOINT` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Vision endpoint. |
| `WO_VISION_MODEL` | `qwen-vl-plus` | Vision model. |
| `WO_VISION_MAX_IMAGES` | `3` | Per vision request image cap. |
| `WO_VISION_MAX_TOKENS` | `800` | Vision output cap. |
| `WO_AGENT_BASE_PROBABILITY` | `0.25` | Per-message probability wake threshold for eligible ambient messages. |
| `WO_AGENT_PROACTIVE_MODE` | `reactive` | `off` disables ambient probability wakeups; `reactive` only joins the current topic; `proactive` may ask a short context-based question or start a light related topic. |
| `WO_AGENT_CONTINUATION_ENABLED` | `True` | Allows the agent to explicitly schedule delayed follow-ups by intent. |
| `WO_AGENT_CONTINUATION_MAX_FOLLOWUPS` | `2` | Max follow-up messages after the source reply. |
| `WO_AGENT_CONTINUATION_DELAY_SECONDS` | `90` | Default delay before reevaluating a scheduled follow-up. |
| `WO_AGENT_CONTINUATION_TTL_SECONDS` | `600` | Expiry window for pending follow-ups. |
| `WO_AGENT_COOLDOWN_SECONDS` | `30` | Per-group probability cooldown. |
| `WO_AGENT_MAX_STEPS` | `8` | Native Phase A max rounds. |
| `WO_AGENT_REFLECT_MAX_STEPS` | `3` | Native Phase B max rounds. |
| `WO_AGENT_REFLECTION_ENABLED` | `True` | Enables chat Phase B memory reflection. |
| `WO_AGENT_PERSONAS_DIR` | `data/personas` | Persona YAML directory. `shared.yaml`, when present, is used for all chats; otherwise per-group cards or `direct-message.yaml` apply. |
| `WO_AGENT_RECENT_CONTEXT_CHAT` | `100` | Initial recent-message window for agent chat. |
| `WO_AGENT_MEMORY_MAX_CHARS` | `100000` | Hard cap for `group_memory`. |
| `WO_AGENT_MAX_TOOL_CALLS_PER_RUN` | `20` | Native Phase A tool budget. |
| `WO_AGENT_MAX_TOOL_CALLS_PER_STEP` | `4` | Native Phase A per-step tool budget. |
| `WO_AGENT_MAX_IMAGE_READS_PER_RUN` | `2` | Native image-read budget. |
| `WO_AGENT_MAX_VOICE_READS_PER_RUN` | `2` | Native voice-read budget. |
| `WO_EXPRESSION_PACK_ENABLED` | `True` | Enable the fixed expression pack, model selection, and `/expression`. |
| `WO_AGENT_LURK_ENABLED` | `False` | Enables dispatcher auto-lurk. |
| `WO_AGENT_LURK_INTERVAL_SECONDS` | `1800` | Auto-lurk scan interval. |
| `WO_AGENT_LURK_MIN_NEW_MESSAGES` | `20` | Minimum new messages before auto-lurk. |
| `WO_AGENT_LURK_RECENT_MSGS` | `100` | Max messages processed in one lurk run. |
| `WO_AGENT_LURK_MAX_STEPS` | `4` | Lurk tool-calling rounds. |
| `WO_OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | OpenClaw gateway. |
| `WO_OPENCLAW_TOKEN` | empty | OpenClaw gateway token. |
| `WO_OPENCLAW_AGENT_ID` | `wechat-bot` | OpenClaw agent id. |
| `WO_OPENCLAW_TIMEOUT_SECONDS` | `300` | OpenClaw gateway request timeout. |
| `WO_AGENT_BACKEND` | `native` | `native` or `openclaw`. |
| `WO_REPLY` | `True` | Send replies back to WeChat. |
| `WO_REPLY_BACKEND` | `wx4py` | `wx4py` or `stdout`. |
| `WO_REPLY_MENTION_POLICY` | `explicit` | `always` mentions the requester on every group reply; `explicit` mentions only direct/command/reply triggers; `never` sends plain group messages. |

`WO_WHISPER_MODEL` is read directly by the mm worker and defaults to `small`; accepted values depend on faster-whisper, commonly `tiny`, `base`, `small`, `medium`, and `large-v3`.

## Logs and Debugging

- `data/dispatcher.log`: compact human-readable command and agent traces.
- `data/llm_debug.log`: LLM prompts, raw replies, parsed results, and full traces.
- `data/events.jsonl`: lightweight machine-readable lifecycle events for ingest batches, trigger decisions, command/agent runs, native tool calls, memory writes, and reply attempts. Use this to answer operational questions such as "why did this message trigger?" or "where did the latency go?" without reading full prompts.
- `data/*.process.log`: rotating loguru process logs for long-running processes such as `live` and `dispatcher`.
- `data/openclaw.log` / `data/mcp.log`: JSONL audit logs for OpenClaw gateway calls and MCP tool invocations.
- `wx4py_send_audit.jsonl`: wx4py send audit records when wx4py emits them.

Useful checks:

```powershell
uv run wechat-oracle status
uv run wechat-oracle verify roundtrip
uv run wechat-oracle agent show-runs <group_id> -n 20
```

`verify roundtrip` confirms whether WeFlow echoes the bot's own wx4py-sent messages back into `messages`. That echo is required for bot wxid auto-discovery and reply-to-bot triggers.

## Known Limits

- wx4py depends on a visible Windows desktop and WeChat Qt UI. It is not an official WeChat API.
- Group reply delivery is implemented only through `wx4py` or local `stdout`.
- OpenClaw is supported as an agent runtime backend, not as a group-message reply backend.
- If WeFlow does not expose historical media files, old images/voices imported by backfill cannot be OCRed/transcribed until you provide media.
- Video content is stored as metadata/path only; video understanding is not implemented.
- Deduplication is best-effort when WeFlow lacks `wx_msg_id`.

## Development

Classroom regression tests use Python unittest (no pytest dependency). There is no ruff, mypy, or CI configuration. Use the local checks below before committing:

```powershell
uv run python -m compileall src\wechat_oracle
uv run python -m unittest discover -s tests -v
uv run wechat-oracle agent --help
uv run wechat-oracle status
uv run python .Codex\hooks\check_doc_sync.py
git diff --check
```

Enable the git hook once per clone:

```powershell
git config core.hooksPath .githooks
```

When changing schema, CLI commands, config fields, or dispatcher command syntax, update this README in the same commit.


## Krea2 与班级日程

- `/krea 一只在图书馆看书的白猫，水彩画`：生成一张 PNG 附件并发回本群。直接 @／回复也能调用 generate_group_image；后台潜水、主动插话、延后追发不能生图。

- `/schedule 下周一上午9点，全班在教学楼301开班会`：提取时间、人物、地点，保存后回显日程编号与提醒时间。默认北京时间前一天19:00和当天07:00；活动早于07:00则当天提前一小时（不早于当天00:00）。可在描述里指定提醒时刻。日期或开始时间不清楚会追问；人物、地点可标为待定。
- `/schedule list`：查看本群未来日程及提醒状态。
- `/schedule cancel 3`：取消自己在本群创建的日程。修改可取消后重新登记。

图片发送使用受控文件剪贴板和微信“粘贴”菜单，再核对本群、机器人账号、发送时间以及微信服务器消息ID的图片/对应文件回声，才报告发送成功。失败或状态不明时保留生成的PNG，不自动重发或重新生成；原件可由部署端核对后补发。已有输入草稿、群身份不符或弹窗不属于微信进程时拒绝发送。

提醒持久化于 class_events / class_reminders，发送共用串行 wx4py 通道。创建时已过的前一天提醒跳过；已到的当天提醒尽快补发。重启时只补仍未开始的活动，同一活动多条过期提醒合并为一条。发送失败记 failed，发送途中崩溃记 uncertain，不自动重试以免重复；list 可查状态。微信、WeFlow 和 dispatcher 必须持续运行。当前只支持单次日程，周期性日程需逐次登记。微信文件读取与处理暂未启用。

Krea2 默认1024×1024、8步、每个直接聊天轮次一个任务。输出位于 data/agent_outputs/<group_id>/（WO_AGENT_OUTPUT_DIR）。只调用固定 http://127.0.0.1:18032（WO_KREA2_BASE_URL）；凭据只读 C:/ProgramData/DevMigration/Krea2ApiKey（WO_KREA2_KEY_FILE），不存入 .env。部署设置 SSH 本地转发。WO_KREA2_TIMEOUT_SECONDS=180、WO_KREA2_POLL_SECONDS=2、WO_KREA2_MAX_IMAGE_BYTES=10000000、WO_KREA2_MAX_PROMPT_CHARS=1000、WO_KREA2_MAX_STEPS=30；实现另有硬上限。WO_AGENT_OUTPUT_MAX_BYTES=1000000 保留为文本附件发送上限。

### 固定表情包

`WO_EXPRESSION_PACK_ENABLED=True` 时，11 张已有 PNG 构成固定图库：`default`、`greeting`、`surprised`、`shy`、`closed-eye-smile`、`pouting`、`proud-wink`、`explaining`、`reading`、`thinking`、`tea`。`/expression list` 列出编号和语义；`/expression <编号>` 先发文字确认，再发对应图片，便于验收发送链路。

直接 @ 或回复 bot 时，native agent 的 `send_expression_image` 工具只接收一个已批准的语义编号，按完整回复的语气最多选一张；模型看不到文件路径或群 ID。OpenClaw 模式可在最终回复末尾附加一行 `[[expression:<编号>]]`，程序会剥离这一行并按同一图库选图；编号嵌在正文、重复或无效时均不发图。图片发送排在文字回复成功之后，共用串行 wx4py 粘贴菜单和 WeFlow 服务器回声确认；配图失败不影响已成功的文字回复，也不会自动重发图片。后台插话、潜水、反思、Local Ask 和普通 slash 命令不会自行选图。

图片来自 `bilibili-ai-bot` 的 Occamy 表情图库，来源与 CC BY-NC-SA 4.0 署名说明随包保留于 `src/wechat_oracle/assets/expressions/CREDITS.md`。这些是微信普通图片消息，不是微信表情面板里的自定义 sticker，也不是朋友圈发布。


## 班级通知、待办与看板

`/notice` 可在引用一条通知后直接发送，也支持 `/notice <通知正文>`。每次最多提取8项活动或截止事项，先生成24小时有效的草稿，不会自动安排提醒。核对日期、时间、人物、地点与提醒时刻后，由提取者逐条发送 `/notice confirm <草稿编号>`；放弃用 `/notice dismiss <草稿编号>`，查看自己的待确认草稿用 `/notice list`。确认仅限同一群的草稿创建者，重复确认不会重复建档；日期时间不明确会追问。

`/todo 本周五17点前，全班提交实验报告` 直接登记待办及截止时间，沿用前一天19:00与当天07:00的提醒规则（北京时间）。`/todo list` 显示未来及最近30天截止的待办，最多20项；`/todo done 3` 只记录发消息者本人确认完成，`/todo undo 3` 撤回本人确认。`/todo status 3` 显示本人主动确认完成的人数及姓名，未确认不等于未完成；`/todo cancel 3` 仅限创建者取消整条待办，不会替他人标记完成。

`/today` 汇总本群今天已登记活动与截止事项，并列出最近30天已过截止时间的待办；`/week` 汇总北京时间本周一到周日的已登记事项。看板最多显示30项，今日额外显示最多10项近期逾期记录；未确认通知草稿不计入看板。看板按需查询，不会自动向群里推送。

数据：class_events.kind 区分 event/todo（老记录迁移为event）；class_notice_drafts 保存提取草稿、来源消息和确认后的事项编号；class_todo_reports 只保存本人明确上报的完成状态。提醒仍由 class_reminders / ReminderWorker 处理。全部查询、确认和取消均绑定当前群；不从聊天推测个人提交情况。文件处理仍未启用。


## 班委自然语言管理与工具回执

检索、历史查找、班级看板和已登记事项查询向所有群成员开放，不需要班委身份。native agent 新增 query_classroom，读取真实数据库后直接展示工具结果。原有 /find、/sum 等及 slash 命令继续可用，权限没有因此收紧。

班委名单位于 data/class_officers.json（跟随 WO_DATA_DIR），格式为 {"groups":{"群wxid@chatroom":["班委账号wxid"]}}。按群ID和真实发送者账号校验，不按昵称；文件缺失、无效或名单为空时，不开放自然语言管理工具。名单由部署端手动配置，模型不能修改；每次调用与确认都会重新检查，撤销名单即时生效。

已配置的班委 @ 或回复机器人后，模型可调用 propose_class_action，准备日程、截止待办、取消本人事项、确认/放弃本人通知草稿或生图方案。程序校验参数，展示工具、时间、人物、地点、提醒安排与预期效果，并保存10分钟有效的方案（尚未执行）。本人再 @ 或回复“确认执行 P编号”，程序执行原方案，不重新调用模型解析参数；“取消执行 P编号”只取消方案。省略编号时只匹配本人在本群最近的待确认方案。提出修改时读取 my_plan 后生成新方案，旧方案作废；提醒边界已到需重新核对。

class_action_plans 保存原始参数、展示内容、发起/执行消息、状态和结果。数据库写入与完成回执在同一事务中提交；重复确认不会重复执行。图片外部任务先标 executing，失败或中断不自动重试，以避免重复生成/发送。自然语言管理回执由实际工具结果生成，替代模型自由改写；没有调用工具却声称已登记等常见虚假成功表述会被拦截。明确的非班委写请求由程序提前拒绝，查询措辞不拦截；工具已返回结果而后续模型服务出错时仍展示实际回执。这个机制不等于证明模型总能选择正确工具，选工具能力需用隔离实验验证。

extract_class_notice 仅能把通知数据写成草稿，不能借正文中的 confirm 等词绕过确认；query_classroom 对所有人开放。背景潜水、概率插话、延后追发拿不到这些管理入口。对已授权班委，自然语言生图也走方案确认；明确 /krea 命令保留直接调用行为。课程表、报名和文件处理不在本次范围。

## 来源、版本与资料检索

- `/find` 只展示实际候选中的消息编号、发送者、完整日期与原文；没有命中时说明本次查询未检索到依据，不能据此断言群里从未发过。
- `/today`、`/week` 标明来源文档版本、原始/发起消息编号和原文摘录。来源缺失明确标注；模型提取的字段与原文可互相核对。
- `/kb find 小组汇报的PPT要求`：关键词＋Qwen3语义检索本群当前已发布版本；展示来源和关联事项的有效、已过时间或已取消状态。命中历史群消息不代表已登记日程。
- `/kb show D3`、`/kb show D3 v1`、`/kb versions D3`：读取当前内容或历史已发布版本。`/kb event 3` 查看事项来源与关联资料。
- 班委可用 `/kb add 课程 | 标题 | 正文或链接`、`/kb revise D3 课程 | 标题 | 新正文`、`/kb link 事项编号 D3`。只保存正文或链接，不下载文件；修订限本人创建的资料。自然语言资料操作先生成方案，由本人确认后写入。
- `/notice revise 事项编号 新通知正文`：为本人事项生成修订草稿，确认前原事项仍有效；确认时原子替换旧事项及未发送提醒。通知原文、版本和旧事项保留。

knowledge_documents/revisions 为真实内容和版本来源，knowledge_event_links 关联日程、待办、通知与资料；FTS及向量只是派生索引。后台持续索引已授权群消息，包括后续OCR/ASR更新。长消息原文保留，前16000字分块索引。索引未完成、语义接口故障均在结果中说明；群隔离在候选查询前实施，旧向量不会覆盖当前版本。

明确“某时准时提醒”的自然语言请求走日程字段提取并生成待确认方案，禁止落入对话延后追发；时间/日期含糊则追问。准时提醒的当天触发时刻等于指定时刻，允许10分钟内恢复补发；普通活动仍保留前一天与当天的提前提醒。class_reminders 记录 last_attempt_at/sent_at，发送状态以实际回执为准。新成员不会自动进入班委名单，按部署时成员账号快照授权。

实时入口除SSE外还有同进程轮询兜底；只读取配置群，水位持久化，启动补归档的旧消息不自动执行历史请求。

Windows Qt 输入兼容：群搜索与普通文本发送读取并核对 UIA ValuePattern 的真实值；只选择名字精确匹配的群搜索结果，点击投递前验证弹窗属于同一微信进程，并等待搜索框清空。发送按键投递到已连接的微信 HWND，等待输入框清空。发现已有草稿时拒绝覆盖；发送状态不明确时不自动重试。真实 @ 选择失败可退为普通文本 @。命令计算完成但回复发送失败时，command_runs 记录 error，不能算作送达。

## RNAHarness 微信输入适配

对 `WO_RNAHARNESS_GROUPS` 指定群的直接 @/回复，只有明确科研意图的请求提交给开发机既有 RNAHarness gateway（`POST /messages`）：RNA/DNA 名称或序列、PDB/三维结构、RNA 折叠、科研 worker、文献证据、实体解析、计算工具目录等；显式写出 `RNAHarness` 也可指定这条路。其他直接 @/回复沿用常规群聊 agent，能读取群上下文并使用原有群工具。读取 `GET /tasks/<id>` 中的真实事件。收到任务号后发送确认，工具执行中节流展示最多3条进度，最终结果仍经过原有微信串行发送通道。任务失败、空结果、服务重启或记录过期会明确报告；不会把半截 delta 当最终答案，也不会在失败后静默换用另一套模型回答。

当前接入的既有内核是使用本地 Occamy 的 RNA 研究 agent，提供 RNAHarness 的科研工具。WeChat-Oracle 继续负责群管理工具：显式 `/schedule`、`/todo`、`/notice`、`/kb`、`/krea` 等命令保持；包含日程、待办、提醒、群资料、生图等明确本地操作的自然语言也保留既有流程。主动插话、潜水与延后追发沿用原处理路径，不自动启动科研任务。

常规 agent 里的 `rna_cloud` 使用混合执行策略。短任务、PubMed、ClinicalTrials.gov 和 openFDA 工具通过 AgentArts v11 MCP；`vienna`、`rhofold`、`ncbp`、`gatsite`、`gemorna` 和 `rnadvisor` 通过本机 RNAHarness adapter 端到端执行并只返回一次最终结果。云端返回空事件或本机执行失败时都会返回结构化错误，不会自动重提可能已经提交的任务。

`rnaharness_requests` 保存来源群/消息、任务号、真实结果、确认/进度发送计数及错误码。微信账号与群ID派生独立 gateway 会话，不混入其他群或网页会话。原始来源与群ID不匹配时拒绝提交。同一来源消息已完成则复用本地回执；POST 超时/不确定时不重提；已有任务恢复只查询原任务号。gateway 的内存任务重启或过期无法恢复时报告状态不明，不声称取消或完成。完整文本在本地保存，群内超过6000字明确标记截断。

三维结构请求可返回 RhoFold 的 PDB 附件：仅接收当前任务的真实产物事件，再从固定本机 worker 核对已完成作业、文件清单与原子坐标，另存到当前群输出目录。附件通过原有串行 sender 发送，以 WeFlow 的服务器文件回声确认；`rnaharness_artifacts` 记录发送状态，状态不确定时不自动重发。二级结构 dot-bracket 不能代替 PDB，三维文件属于模型预测。

明确包含一条RNA序列并要求输出PDB的简单请求，由内核直接执行三维工具并生成真实作业回执，避免模型生成失常阻断计算。否定、能力咨询、历史查找、引文和多序列/复杂比较请求不走此捷径。模型的重复乱码即使触及长度上限也会停止，不继续续写；对应错误为MODEL_DEGENERATED。

## License

MIT. See [LICENSE](LICENSE).
