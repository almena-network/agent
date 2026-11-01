"""Mounts the A2A interface on the app: the agent card and both bindings.

The card is public; both bindings sit behind ``auth.A2AAuth``, which needs a
registry token on every call.
"""

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from fastapi import FastAPI

from almena_agent.a2a.auth import A2AAuth
from almena_agent.a2a.card import JSONRPC_PATH, REST_PATH, build_agent_card
from almena_agent.a2a.executor import ClaudeAgentExecutor
from almena_agent.a2a.metered import MeteredRequestHandler
from almena_agent.a2a.tasks import BoundedTaskStore
from almena_agent.accounts import Accounts
from almena_agent.config import Settings
from almena_agent.conversations import Conversations
from almena_agent.llm import Model
from almena_agent.registry import Registry


def add_a2a_routes(
    app: FastAPI,
    settings: Settings,
    model: Model,
    registry: Registry | None,
    accounts: Accounts,
) -> None:
    card = build_agent_card(settings)
    conversations = Conversations(
        settings.history_turns,
        idle_seconds=settings.conversation_idle_minutes * 60,
        max_contexts=settings.max_conversations,
    )
    # Counted by method and outcome (agent_a2a_requests).
    handler = MeteredRequestHandler(
        DefaultRequestHandler(
            agent_executor=ClaudeAgentExecutor(model, conversations, registry),
            # Tasks live in memory: lost on restart, not shared between replicas,
            # finished ones dropped once idle or beyond the cap. Each belongs to
            # the account that sent it (the request's user, set by A2AAuth):
            # nobody else gets, lists, cancels or follows it.
            task_store=BoundedTaskStore(
                idle_seconds=settings.task_idle_minutes * 60,
                max_tasks=settings.max_tasks,
            ),
            agent_card=card,
        )
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=JSONRPC_PATH),
        rest_routes=create_rest_routes(handler, path_prefix=REST_PATH),
    )
    app.add_middleware(A2AAuth, settings=settings, accounts=accounts)
