from httpx import AsyncClient


async def test_agent_card_advertises_the_public_endpoints(client: AsyncClient) -> None:
    response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Almena Agent"
    assert card["capabilities"]["streaming"] is True
    interfaces = {i["protocolBinding"]: i["url"] for i in card["supportedInterfaces"]}
    assert interfaces == {
        "JSONRPC": "https://agent.almena.id/a2a/jsonrpc",
        "HTTP+JSON": "https://agent.almena.id/a2a/rest",
    }
    assert [skill["id"] for skill in card["skills"]] == ["almena-assistant", "almena-registry"]
    scheme = card["securitySchemes"]["almenaRegistry"]["httpAuthSecurityScheme"]
    assert scheme["scheme"] == "Bearer"
    # The token is required on every call.
    assert card["securityRequirements"] == [{"schemes": {"almenaRegistry": {}}}]


async def test_the_card_needs_no_token(anonymous: AsyncClient) -> None:
    response = await anonymous.get("/.well-known/agent-card.json")
    assert response.status_code == 200
