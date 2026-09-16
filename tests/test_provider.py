"""Retry, backoff, классификация ответов. httpx мокается через respx."""

import httpx
import pytest
import respx
from src import provider as prov
from src.config import settings as app_settings


@pytest.fixture(autouse=True)
def fast_retry():
    """Ускоряем backoff, чтобы тесты не спали."""
    old = (
        app_settings.provider_max_attempts,
        app_settings.provider_backoff_base_s,
        app_settings.provider_backoff_cap_s,
    )
    app_settings.provider_max_attempts = 3
    app_settings.provider_backoff_base_s = 0.001
    app_settings.provider_backoff_cap_s = 0.005
    yield
    (
        app_settings.provider_max_attempts,
        app_settings.provider_backoff_base_s,
        app_settings.provider_backoff_cap_s,
    ) = old


@respx.mock
async def test_503_retried_until_success():
    route = respx.post("http://provider.test/payments").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(202, json={"providerPaymentId": "pp-1", "status": "ACCEPTED"}),
        ]
    )

    result = await prov.submit_payment("op-1", "100.00", "RUB")

    assert result.outcome is prov.ProviderOutcome.ACCEPTED
    assert result.provider_payment_id == "pp-1"
    assert route.call_count == 3


@respx.mock
async def test_400_not_retried():
    route = respx.post("http://provider.test/payments").mock(
        return_value=httpx.Response(400, text="bad request")
    )

    result = await prov.submit_payment("op-2", "100.00", "RUB")

    assert result.outcome is prov.ProviderOutcome.PERMANENT
    assert route.call_count == 1


@respx.mock
async def test_network_error_returns_retryable():
    respx.post("http://provider.test/payments").mock(side_effect=httpx.ConnectError("boom"))

    result = await prov.submit_payment("op-3", "100.00", "RUB")

    assert result.outcome is prov.ProviderOutcome.RETRYABLE


@respx.mock
async def test_idempotency_key_sent():
    route = respx.post("http://provider.test/payments").mock(
        return_value=httpx.Response(202, json={"providerPaymentId": "pp-1", "status": "ACCEPTED"})
    )

    await prov.submit_payment("op-with-key", "100.00", "RUB")

    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == "op-with-key"
    assert request.headers["X-Correlation-ID"] == "op-with-key"
