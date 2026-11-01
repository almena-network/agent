# Almena Agent

The AI agent of Almena ID, served at `https://agent.almena.id`. It
answers with Claude (Anthropic) and is reached by other agents over the
[Agent2Agent (A2A) protocol](https://a2a-protocol.org) 1.0, through its agent
card and two bindings: JSON-RPC and HTTP+JSON (REST).

Built with Python 3.13, [a2a-sdk](https://pypi.org/project/a2a-sdk/) 1.1,
the [Anthropic SDK](https://pypi.org/project/anthropic/) 1.9 and FastAPI.

## Quick start

You need [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev) and,
for `task up`, Docker. `task ask` also uses `jq`.

```bash
task init    # .env from .env.example; set ANTHROPIC_API_KEY in it
task dev     # the agent on http://localhost:8100, with auto-reload
task card    # its agent card
ALMENA_TOKEN=almena_… task ask -- "What is a did:web identity?"
task up      # the same, in Docker, behind Caddy at https://$AGENT_DOMAIN
task up:no-caddy  # the same without Caddy, behind ../develop's
task --list  # everything else
```

With `ANTHROPIC_API_KEY` empty, `task dev` uses an `ant auth login` profile.
Every A2A call needs a registry API token (`ALMENA_TOKEN` for `task ask`),
checked against `AGENT_REGISTRY_ACCOUNT_URL`.

Every merge into `main` publishes the image `ghcr.io/almena-id/agent`
(amd64 and arm64) with a `year.month.sequence` version (e.g. `2026.10.1`,
the sequence restarting each month), also tagged `latest` and `sha-<commit>`;
the commit gets the git tag `v<version>`. The image reports that version
(`ALMENA_VERSION`); outside it, the package's. See
[.github/workflows/docker.yml](.github/workflows/docker.yml).

`task up` also starts Caddy, which serves the agent at `https://$AGENT_DOMAIN`
(`agent.almena.id` in `.env.example`) with a Let's Encrypt certificate. Point
the name at this machine (in `/etc/hosts` for development); the certificate
needs the domain's zone in Cloudflare and `CLOUDFLARE_API_TOKEN` (Zone / DNS /
Edit) in `.env`. The agent still answers at `http://localhost:8100`.

## Endpoints

| Method | Path | What |
|--------|------|------|
| GET | `/.well-known/agent-card.json` | The A2A agent card: name, skills, capabilities and the endpoints below, under `AGENT_PUBLIC_URL` |
| POST | `/a2a/jsonrpc` | A2A JSON-RPC 2.0 binding (`SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`…); registry token required |
| * | `/a2a/rest/…` | A2A HTTP+JSON binding (`POST /message:send`, `POST /message:stream`, `GET /tasks/{id}`…); registry token required |
| GET | `/` | The home page for browsers (HTML, in teal): status, version, A2A protocol and the agent card's URL |
| GET | `/fonts/{name}` | The home page's typefaces (Chakra Petch, Inter, JetBrains Mono; woff2) |
| GET | `/.well-known/security.txt` | Where to report a vulnerability ([RFC 9116](https://www.rfc-editor.org/rfc/rfc9116)): this repository's private advisories; `Expires` stays 180 days ahead |
| GET | `/health` | Liveness and the running version |
| GET | `/docs`, `/openapi.json` | API reference (not in `production`) |

Requests to the A2A bindings carry the `A2A-Version: 1.0` header and the
caller's registry token (below).

### How a message is answered

Each message opens a task. Claude's reply streams into it as one text
artifact (`response`), chunk by chunk under `SendStreamingMessage`; the task
ends `COMPLETED`; `REJECTED` when the message has no text, Claude declines
it, or the conversation is full, it belongs to another account or the
registry's MCP endpoint refuses the caller's token; or `FAILED` when Claude cannot be reached
(the cause goes to the log, not to the peer). Messages sharing a `contextId` are one
conversation: Claude sees the turns before (up to `AGENT_HISTORY_TURNS`).

Claude runs with adaptive thinking at `AGENT_EFFORT`, prompt caching, and the
server-side refusal fallback (`fallbacks: "default"`): a request the model
declines for policy reasons is retried on its fallback model within the call.

Tasks and conversations are kept in memory: a restart loses them, and they are
not shared between replicas. A conversation idle for
`AGENT_CONVERSATION_IDLE_MINUTES` is dropped whole, and so is the least
recently used beyond `AGENT_MAX_CONVERSATIONS`; a message to it starts afresh.
A finished task (`COMPLETED`, `FAILED`, `CANCELED` or `REJECTED`) nobody has
read for `AGENT_TASK_IDLE_MINUTES` is dropped, and so are the least recently
used finished ones beyond `AGENT_MAX_TASKS` in all; tasks still in flight are
never dropped. A dropped task is not found, like another account's.

### Authentication and limits

Every call to the bindings carries the caller's Almena registry token,
`Authorization: Bearer almena_…` (an API token made in the registry portal or
with `almena`); only the agent card and the plain routes (`/`, `/health`…)
are public. The card declares the token as the required `almenaRegistry`
scheme. The agent asks the API whose it is (`GET AGENT_REGISTRY_ACCOUNT_URL`,
the signed-in account) and remembers the answer for
`AGENT_TOKEN_CACHE_SECONDS`; without a token the API accepts, Claude never
runs. The refusals are plain HTTP, each with its binding's error body:

| Status | When | JSON-RPC `code` / REST `status` |
|--------|------|---------------------------------|
| `401` + `WWW-Authenticate: Bearer` | No token, or one the API refuses | `-32000` / `UNAUTHENTICATED` |
| `429` + `Retry-After` | More than `AGENT_RATE_LIMIT` calls from one account, or `AGENT_AUTH_FAILURE_LIMIT` refused tokens from one IP, in `AGENT_RATE_LIMIT_WINDOW_SECONDS` | `-32603` / `RESOURCE_EXHAUSTED` |
| `503` + `Retry-After` | The API cannot check a token not yet remembered | `-32603` / `UNAVAILABLE` |

The account is the caller: its tasks are its own (`GetTask`, `ListTasks`,
`CancelTask` and `SubscribeToTask` see no one else's: another account's task
is not found), and so are its conversations: a context stays with the account
that started it, and another one continuing it is rejected, since its history
holds what the registry told the first. A revoked token keeps talking for at
most `AGENT_TOKEN_CACHE_SECONDS`, without the registry's tools.

### The registry's tools

With the caller's token the agent also gets the registry's tools: it is an
MCP client of the API's endpoint (`AGENT_REGISTRY_MCP_URL`) and acts as that
token's account, so it sees and does only what the account may, checked by
the API as any other request; the card lists it as the `almena-registry`
skill. Claude calls the tools as it needs them, up to
`AGENT_MAX_TOOL_ROUNDS` rounds per reply (the last must answer); what it says
between rounds streams into the same artifact. Changes that end in a signature
(signing an identity, issuing) only return the wallet request, which the
signer approves in their wallet; publishing is left to an admin in the portal
or the CLI.

A token the MCP endpoint refuses rejects the task; an unreachable endpoint
leaves the agent without tools for that message, and with
`AGENT_REGISTRY_MCP_URL` empty it only converses (a token is still required).

```bash
curl -s https://agent.almena.id/a2a/rest/message:send \
  -H "A2A-Version: 1.0" -H "Authorization: Bearer $ALMENA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": {"messageId": "1", "role": "ROLE_USER", "parts": [{"text": "What does my tenant still need?"}]}}'
```

## Metrics

With `AGENT_METRICS_ADDR` set, the agent serves Prometheus metrics at
`GET /metrics` on that address, a listener of its own, as the mediator does:
the public app has no `/metrics`, and Caddy only forwards to port 8100, so
they never go out through the proxy. In Docker, set it to `0.0.0.0:9090` and
scrape `agent:9090` from the Compose network; Compose does not publish it.
They only count: no tokens, accounts, contexts or task ids in any label.

| Metric | Labels | What |
|--------|--------|------|
| `agent_a2a_requests_total` | `method`, `outcome` | A2A requests the bindings handled, by method (`SendMessage`, `GetTask`…) and outcome: `ok`, the A2A error (`TaskNotFoundError`…), `error`, or `aborted` (a stream the peer left) |
| `agent_auth_total` | `result` | Tokens checked: `accepted` (by the API), `cached`, `refused`, `missing`, `unavailable` (the API could not answer) |
| `agent_rate_limited_total` | `limit` | `429`s: per `account`, or per `ip` for refused tokens |
| `agent_claude_calls_total` | `outcome` | Requests to Claude, one per round of a reply: `ok`, `refused`, `error` |
| `agent_claude_call_seconds` | — | Histogram of how long each took, streaming included |
| `agent_claude_tokens_total` | `kind` | Tokens Claude reported: `input`, `output`, `cache_read`, `cache_creation` |
| `agent_held` | `kind` | Conversations and tasks (`conversation`, `task`) held in memory now |
| `agent_evictions_total` | `kind`, `reason` | Conversations and finished tasks dropped: `idle` or over the cap (`capacity`) |

Plus the process's own (`process_*`, `python_*`).

## Configuration

`.env` (from `.env.example`), or the environment:

| Variable | Default | What |
|----------|---------|------|
| `AGENT_ENVIRONMENT` | `development` | `development`, `production` (hides `/docs`) or `test` |
| `AGENT_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `AGENT_HOST` | `127.0.0.1` | Interface to listen on (`0.0.0.0` in the image) |
| `AGENT_PORT` | `8100` | Port to listen on (and the one Compose publishes) |
| `AGENT_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted |
| `AGENT_DOMAIN` | — | Domain of the deployment, for Docker Compose and Caddy only |
| `CLOUDFLARE_API_TOKEN` | — | Caddy's DNS-01 challenge (Zone / DNS / Edit) |
| `AGENT_PUBLIC_URL` | `https://agent.almena.id` | Public origin; the agent card's endpoints are under it |
| `AGENT_NAME` | `Almena Agent` | The agent's name in its card |
| `AGENT_DESCRIPTION` | `The AI agent of Almena ID.` | Its description in the card |
| `AGENT_PROVIDER_ORGANIZATION` | `Almena ID` | Who runs it |
| `AGENT_PROVIDER_URL` | `https://almena.id` | Their site |
| `ANTHROPIC_API_KEY` | — | Claude API key (read by the Anthropic SDK) |
| `AGENT_MODEL` | `claude-opus-5-5` | Claude model |
| `AGENT_EFFORT` | `high` | `low`, `medium`, `high`, `xhigh` or `max` |
| `AGENT_MAX_TOKENS` | `64000` | Most tokens per reply |
| `AGENT_SYSTEM_PROMPT` | — | Replaces the built-in prompt (`src/almena_agent/prompts.py`) |
| `AGENT_HISTORY_TURNS` | `50` | Exchanges a conversation holds |
| `AGENT_REGISTRY_MCP_URL` | `https://api.almena.id/mcp` | The registry API's MCP endpoint, used with each caller's token (empty: no tools) |
| `AGENT_MAX_TOOL_ROUNDS` | `20` | Rounds of tool calls one reply may take before it must answer |
| `AGENT_REGISTRY_ACCOUNT_URL` | `https://api.almena.id/api/v1/auth/me` | The registry API's signed-in account: every A2A call's token is checked there (required) |
| `AGENT_TOKEN_CACHE_SECONDS` | `60` | How long an accepted token is trusted before the API is asked again |
| `AGENT_RATE_LIMIT_WINDOW_SECONDS` | `60` | The window both rate limits count in |
| `AGENT_RATE_LIMIT` | `30` | A2A calls one account may make per window |
| `AGENT_AUTH_FAILURE_LIMIT` | `10` | Refused tokens one client IP may send per window (then it waits the window out) |
| `AGENT_CONVERSATION_IDLE_MINUTES` | `60` | Idle time after which a conversation is dropped |
| `AGENT_MAX_CONVERSATIONS` | `1000` | Conversations kept; the least recently used beyond it are dropped |
| `AGENT_TASK_IDLE_MINUTES` | `60` | Idle time (since last saved or read) after which a finished task is dropped |
| `AGENT_MAX_TASKS` | `5000` | Tasks kept in all; the least recently used finished ones beyond it are dropped (in-flight tasks always stay) |
| `AGENT_METRICS_ADDR` | — | Prometheus metrics on their own address (`host:port`, e.g. `127.0.0.1:9090`); empty: off |

## Deployment

The container listens on 8100 behind a TLS-terminating proxy for
`agent.almena.id` that forwards everything to it: this project's Caddy
(`Caddyfile`) when it runs alone; with the whole network, `../develop`'s,
which serves `AGENT_DOMAIN` in its place: both take ports 80 and 443, so start
the agent with `task up:no-caddy`, which leaves this one out.

## License

[Apache License 2.0](LICENSE).
