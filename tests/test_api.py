"""HTTP-контракт: коды, валидация, граничные случаи."""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(db):
    from src.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_create_returns_201(client):
    r = await client.post(
        "/operations",
        json={
            "operationId": "op-1",
            "amount": "100.00",
            "currency": "RUB",
            "description": "x",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "CREATED"
    assert body["providerPaymentId"] is None


async def test_duplicate_returns_409(client):
    payload = {"operationId": "op-2", "amount": "1.00", "currency": "RUB"}
    await client.post("/operations", json=payload)
    r = await client.post("/operations", json=payload)
    assert r.status_code == 409


async def test_invalid_amount_returns_422(client):
    r = await client.post(
        "/operations",
        json={
            "operationId": "op-3",
            "amount": "-1.00",
            "currency": "RUB",
        },
    )
    assert r.status_code == 422


async def test_unsupported_currency_returns_422(client):
    r = await client.post(
        "/operations",
        json={
            "operationId": "op-4",
            "amount": "10.00",
            "currency": "USD",
        },
    )
    assert r.status_code == 422


async def test_submit_202_then_200(client):
    await client.post(
        "/operations", json={"operationId": "op-5", "amount": "1.00", "currency": "RUB"}
    )
    first = await client.post("/operations/op-5/submit")
    second = await client.post("/operations/op-5/submit")
    assert first.status_code == 202
    assert second.status_code == 200


async def test_submit_unknown_returns_404(client):
    r = await client.post("/operations/nope/submit")
    assert r.status_code == 404


async def test_events_unknown_returns_404(client):
    r = await client.get("/operations/nope/events")
    assert r.status_code == 404


async def test_receipt_unknown_returns_404(client):
    r = await client.post(
        "/receipts",
        json={
            "providerPaymentId": "pp",
            "operationId": "nope",
            "result": "COMPLETED",
            "occurredAt": "2026-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 404
