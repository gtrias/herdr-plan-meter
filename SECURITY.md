# Security

Plan Meter reads subscription credentials that belong to other tools, so here is exactly what it touches.

## What it reads

| Credential | Where | When |
|---|---|---|
| Claude Code OAuth access token | macOS Keychain item `Claude Code-credentials-<first 8 hex of sha256(config dir)>`, then `Claude Code-credentials`, via `/usr/bin/security`; or `$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude`) | On each fetch, at most every 30 seconds |
| Codex ChatGPT access token and account id | `$CODEX_HOME/auth.json` (default `~/.codex`) | On each fetch |
| Z.AI API key | `$PI_CODING_AGENT_DIR/auth.json` (default `~/.pi/agent/auth.json`), provider `zai`, field `key` | On each fetch |
| DeepSeek API key | `$DEEPSEEK_API_KEY`, the plugin config's `deepseek_key`, or the `deepseek` `api_key` row in omp's credential store `$OMP_AGENT_DIR/agent.db` (default `~/.omp/agent/agent.db`), opened read-only | On each fetch |

## Where it goes

- Claude: `https://api.anthropic.com/api/oauth/usage`, sent as a `Bearer` token
- Codex: `https://chatgpt.com/backend-api/wham/usage`, sent as a `Bearer` token plus `ChatGPT-Account-Id`
- Z.AI: `https://api.z.ai/api/monitor/usage/quota/limit`, sent in the `Authorization` header
- DeepSeek: `https://api.deepseek.com/user/balance`, sent as a `Bearer` token

Nothing else leaves the machine. Redirects are refused, so a token can never follow a 3xx to another host. There is no telemetry and no third-party server.

## What it never does

- It never writes, caches, logs or prints a token. omp's credential database is opened with `mode=ro`, so even the SQLite hot journal is untouched.
- It never refreshes a token or touches a refresh token.
- It never edits Claude Code, Codex, Pi, or omp credentials, the Keychain, or your herdr `config.toml`. The setup popup only shows and copies a snippet.
- It never makes model requests.

## What it writes

- `$XDG_CACHE_HOME/herdr-plan-meter/` (default `~/.cache/herdr-plan-meter/`): `usage.json` with plan names, percentages and reset times, its temporary `usage.tmp`, and an empty `fetch.lock`.
- The plugin config directory (`herdr plugin config-dir gtrias.plan-meter`): the `tab-bar.sh` shim, written through a temporary `tab-bar.tmp`.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/gtrias/herdr-plan-meter/security/advisories/new) rather than a public issue.
