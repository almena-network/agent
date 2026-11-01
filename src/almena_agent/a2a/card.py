"""The agent card: who the agent is, what it can do and where to reach it.

Served at ``/.well-known/agent-card.json``; peers read it first.
"""

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol

from almena_agent import __version__
from almena_agent.config import Settings

# The caller's Almena registry token, required on every call but the card's:
# the agent answers that account, and operates the registry as it.
REGISTRY_SCHEME = "almenaRegistry"

# Where the two A2A bindings are mounted, under the public origin.
JSONRPC_PATH = "/a2a/jsonrpc"
REST_PATH = "/a2a/rest"


def build_agent_card(settings: Settings) -> AgentCard:
    origin = settings.public_url.rstrip("/")
    return AgentCard(
        name=settings.name,
        description=settings.description,
        version=__version__,
        provider=AgentProvider(
            organization=settings.provider_organization, url=settings.provider_url
        ),
        # The first interface is the preferred one.
        supported_interfaces=[
            AgentInterface(
                url=f"{origin}{JSONRPC_PATH}",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            ),
            AgentInterface(
                url=f"{origin}{REST_PATH}",
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            ),
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        security_schemes={
            REGISTRY_SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer",
                    bearer_format="Almena registry API token (almena_…)",
                    description=(
                        "Required. An API token of the Almena registry: the agent "
                        "answers its account and operates the registry as it."
                    ),
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={REGISTRY_SCHEME: StringList()})],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "text/markdown"],
        skills=[
            AgentSkill(
                id="almena-assistant",
                name="Almena ID assistant",
                description=(
                    "Answers questions about Almena ID: its identities, "
                    "issuers, verifiers, mediators and registry."
                ),
                tags=["almena", "identity", "did", "didcomm"],
                examples=["What is a did:web identity in Almena ID?"],
            ),
            AgentSkill(
                id="almena-registry",
                name="Almena registry operator",
                description=(
                    "Operates the caller's tenants in the Almena registry with their "
                    "registry token: reads and sets up issuers, verifiers, mediators, "
                    "identities, domains, forms and applications, and prepares the "
                    "wallet requests their signers approve."
                ),
                tags=["almena", "registry", "mcp"],
                examples=[
                    "What does my tenant still need set up?",
                    "Register an issuer called Town Hall and tell me what is left to publish it.",
                ],
            ),
        ],
    )
