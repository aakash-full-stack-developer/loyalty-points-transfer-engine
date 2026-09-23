import httpx
import pytest

pytestmark = pytest.mark.usefixtures("seeded")


async def test_accounts_show_balances_with_masked_member_ids(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/accounts", headers={"X-User-Id": "user_alice"})

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "user_alice"
    accounts = {account["program"]: account for account in body["data"]}
    assert set(accounts) == {
        "NOVA_REWARDS",
        "ZENITH_POINTS",
        "SKYWARD_MILES",
        "STAYWELL_POINTS",
        "HARBOR_CRUISE_POINTS",
    }
    assert accounts["NOVA_REWARDS"]["balance"] == 250_000
    assert accounts["SKYWARD_MILES"]["external_member_id"] == "****0301"
    assert "SKY100200301" not in response.text


async def test_users_only_see_their_own_accounts(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/accounts", headers={"X-User-Id": "user_bob"})

    accounts = {account["program"]: account for account in response.json()["data"]}
    assert accounts["NOVA_REWARDS"]["balance"] == 50_000
    assert accounts["NOVA_REWARDS"]["external_member_id"] == "****0002"
    assert accounts["SKYWARD_MILES"]["external_member_id"] == "****0302"


async def test_unknown_user_has_no_accounts(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/accounts", headers={"X-User-Id": "user_nobody"})

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.parametrize("headers", [{}, {"X-User-Id": "not a valid id!"}], ids=["missing", "bad"])
async def test_user_identity_is_required(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/v1/accounts", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
