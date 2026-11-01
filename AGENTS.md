# AGENTS.md

Guidance for coding agents working on `agent`.

## Layout

```
src/almena_agent/
  __main__.py        entry point (`almena-agent`): runs uvicorn with the settings
  main.py            FastAPI app factory (`create_app(model, registry, accounts)`): health, A2A routes, Scalar docs
  config.py          Settings (pydantic-settings, AGENT_* variables)
  metrics.py         Prometheus metrics (`METRICS`), served by their own listener
                     (`AGENT_METRICS_ADDR`, opened in the app's lifespan), never by the app
  prompts.py         the built-in system prompt
  llm.py             `Model`, what the executor needs from the model, and `Claude`,
                     its implementation (streaming, adaptive thinking, caching,
                     refusal fallback, the loop over the registry's tools); `get_model` builds it
  accounts.py        whose a registry token is: `Accounts`, `ApiAccounts` asks the API's
                     signed-in-account endpoint
  registry.py        the registry's tools: `Registry` opens a `Toolbox` with the caller's
                     token, `McpRegistry` is the MCP client of the API's `/mcp`
  conversations.py   history per A2A context, in memory, append-only, owned by its caller's
                     account; idle and least recently used contexts are dropped whole
  a2a/auth.py        `A2AAuth`, the middleware before both bindings: registry token required,
                     checked (and briefly cached), the account set as the request's user;
                     rate limits per account and per IP for refused tokens
  a2a/card.py        the agent card and the bindings' paths
  a2a/executor.py    `ClaudeAgentExecutor`: message → task → streamed artifact → final state
  a2a/tasks.py       `BoundedTaskStore`, the a2a-sdk's in-memory store, owner-scoped, with
                     idle and least recently used finished tasks dropped (in-flight ones kept)
  a2a/metered.py     `MeteredRequestHandler`, the a2a-sdk's handler wrapped to count requests
  a2a/routes.py      mounts the card, JSON-RPC and REST routes (a2a-sdk) on the app
  api/routes/        plain HTTP routes outside A2A (health.py; home.py, the browser page
                     at `/` and its typefaces at `/fonts/`; well_known.py, security.txt)
  assets/            home.html (a `string.Template`), home.css and the portals'
                     typefaces (`fonts/`, SIL OFL, licences beside them)
tests/               pytest (async, httpx ASGITransport); conftest's FakeModel stands in for Claude,
                     FakeRegistry for the registry, FakeAccounts for the API's accounts
```

## Rules

- Teal `#0fa3a3` is the agent's identity: `--brand` in `assets/home.css` and
  `BRAND` in `api/routes/home.py` (the favicon), carried by the mark in the header
  and footer. Green carries the status only.

- Everything is written in English: code, comments, docs, commit messages.
- Use `task` for everything (`task --list`); `task check` must pass before finishing.
- mypy runs in strict mode: type every function.
- Tests never call Claude or the registry: they pass a fake `Model`, `Registry`
  and `Accounts` to `create_app`.
- Every A2A call needs a registry token the API accepts; without one Claude
  never runs. Only the agent card, `/`, `/health` and the other plain routes are public.
- Tasks and conversations belong to the caller's account (the a2a-sdk scopes
  tasks by the request's user, which `A2AAuth` sets): never key them by anything else.
- The agent never holds a registry token of its own: its tools act only with the
  caller's, for that message.
- Conversation history is append-only: never edit or trim turns already sent
  to Claude (it invalidates its thinking blocks).
- A2A types are protobuf messages from `a2a.types`; build them with
  `a2a.helpers` and `TaskUpdater` rather than by hand.
- New skills go in the agent card (`a2a/card.py`) and the README.
- New settings get an `AGENT_` variable in `config.py`, `.env.example`,
  `compose.yml` and the README table.
- New endpoints come with tests and keep the README endpoint table current.
- Metrics stay on their own listener, never on the public app; their labels
  never hold tokens, accounts, context or task ids. New ones go in the README's
  metrics table.
