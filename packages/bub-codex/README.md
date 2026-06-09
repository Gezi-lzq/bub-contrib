# bub-codex

Codex model plugin for `bub`.

## What It Provides

- Bub plugin entry point: `codex`
- A `run_model` hook implementation backed by the `openai-codex` Python SDK
- Session continuation through Bub tape `codex/handoff` anchors, with Codex thread
  id stored only as an optional resumable pointer
- Optional temporary skill wiring from `skills` into workspace `.agents/skills`

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-codex"
```

You can also install it with Bub:

```bash
bub install bub-codex@main
```

## Prerequisites

- Install the SDK extra with prereleases enabled:

  ```bash
  uv pip install --prerelease=allow "bub-codex[sdk]"
  ```

  The extra pins `openai-codex==0.1.0b3`, which resolves to
  `openai-codex-cli-bin==0.137.0a4`. This is currently the first tested SDK
  release in this spike that installs on Linux x86_64 glibc environments.
- Codex should be authenticated before runtime.

Package note: `openai-codex<=0.1.0b2` depends on
`openai-codex-cli-bin==0.132.0`, whose published wheels do not include this
Linux x86_64 glibc platform. `openai-codex==0.1.0b3` depends on the prerelease
runtime `openai-codex-cli-bin==0.137.0a4`, so installers must allow prereleases.
The plugin's normal backend path imports and uses `openai-codex`; there is no
`codex e` subprocess fallback.

## Configuration

The plugin reads environment variables with prefix `BUB_CODEX_`:

- `BUB_CODEX_MODEL` (optional): model override passed as `--model <value>`
- `BUB_CODEX_YOLO_MODE` (optional, default: `false`): when `true`, requests the
  SDK full-access sandbox preset; otherwise requests workspace-write.
- `BUB_CODEX_TIMEOUT_SECONDS` (optional): turn timeout passed to the SDK when set.
- `BUB_CODEX_RESUME_THREADS` (optional, default: `true`): when enabled, the
  adapter may resume the optional Codex thread id recorded on the latest
  `codex/handoff` anchor.

## Runtime Behavior

- Workspace resolution:
  - Uses `state["_runtime_workspace"]` when present
  - Falls back to current working directory
- Normal turns call the `openai-codex` SDK instead of spawning `codex e`.
- Completed turns write a compact `codex/handoff` tape anchor containing the Bub
  session id, tape name, cwd, optional Codex thread/turn ids, response summary,
  status, and steering count.
- On the next turn, the adapter may resume the optional Codex thread id from the
  latest handoff anchor. If no thread id is available, it starts a fresh Codex
  thread and prepends the previous handoff summary as minimal continuation
  context.
- While a Codex turn is active, `admit_message` steers new messages into the
  running turn when Bub reports steering support; otherwise those messages wait
  as follow-up work. The plugin does not allow concurrent turns for the same
  session by default.
- Direct app-server JSON-RPC should remain below the `CodexSdkBackend` adapter if
  the SDK lacks a needed steering, streaming, or approval capability.

## Skill Integration

- During invocation, the plugin scans `skills` for directories containing `SKILL.md`.
- It creates symlinks under `<workspace>/.agents/skills/<skill_name>`.
- Symlinks created by this plugin invocation are removed after the run.
