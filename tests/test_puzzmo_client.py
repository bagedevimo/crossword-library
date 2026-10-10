import json
from pathlib import Path

import pytest
import requests
import responses

from crossword_library.puzzmo import API_URL, PuzzmoAPIError, PuzzmoAuthError, PuzzmoClient

CALENDAR = json.loads((Path(__file__).parent / "fixtures" / "calendar_2026_10.json").read_text())


def make_client(**kwargs):
    sleeps = []
    client = PuzzmoClient("secret-token", sleep=sleeps.append, **kwargs)
    return client, sleeps


@responses.activate
def test_fetch_month_sends_zero_based_month_and_auth():
    responses.post(API_URL, json=CALENDAR)
    client, _ = make_client()
    entries = client.fetch_month(2026, 10)
    assert len(entries) == 5
    request = responses.calls[0].request
    body = json.loads(request.body)
    assert body["variables"] == {"year": 2026, "month": 9}
    assert body["operationName"] == "CalendarQuery"
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.headers["auth-provider"] == "custom"


@responses.activate
def test_http_401_is_auth_error_without_retry():
    responses.post(API_URL, status=401, json={})
    client, sleeps = make_client()
    with pytest.raises(PuzzmoAuthError, match="PUZZMO_TOKEN"):
        client.fetch_month(2026, 10)
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_graphql_login_error_is_auth_error():
    responses.post(API_URL, json={"errors": [{"message": "Must be logged in"}], "data": None})
    client, _ = make_client()
    with pytest.raises(PuzzmoAuthError):
        client.fetch_month(2026, 10)


@responses.activate
def test_server_errors_retry_with_backoff_then_succeed():
    responses.post(API_URL, status=502)
    responses.post(API_URL, status=503)
    responses.post(API_URL, json=CALENDAR)
    client, sleeps = make_client(backoff_seconds=1.0)
    assert len(client.fetch_month(2026, 10)) == 5
    assert sleeps == [1.0, 2.0]


@responses.activate
def test_gives_up_after_retries():
    for _ in range(4):
        responses.post(API_URL, status=500)
    client, sleeps = make_client(retries=3)
    with pytest.raises(PuzzmoAPIError, match="2026-10 failed after 4 attempts: HTTP 500"):
        client.fetch_month(2026, 10)
    assert len(responses.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


@responses.activate
def test_network_errors_are_retried():
    responses.post(API_URL, body=requests.ConnectionError("boom"))
    responses.post(API_URL, json=CALENDAR)
    client, sleeps = make_client()
    assert len(client.fetch_month(2026, 10)) == 5
    assert sleeps == [1.0]


@responses.activate
def test_unexpected_status_is_api_error():
    responses.post(API_URL, status=400, body="bad request")
    client, _ = make_client()
    with pytest.raises(PuzzmoAPIError, match="HTTP 400"):
        client.fetch_month(2026, 10)


@responses.activate
def test_non_json_is_api_error():
    responses.post(API_URL, body="<html>oops</html>")
    client, _ = make_client()
    with pytest.raises(PuzzmoAPIError, match="non-JSON"):
        client.fetch_month(2026, 10)


@responses.activate
def test_token_not_in_error_messages():
    for _ in range(4):
        responses.post(API_URL, status=500)
    client, _ = make_client()
    with pytest.raises(PuzzmoAPIError) as excinfo:
        client.fetch_month(2026, 10)
    assert "secret-token" not in str(excinfo.value)


@responses.activate
def test_malformed_calendar_entry_is_api_error_without_token():
    payload = json.loads(json.dumps(CALENDAR))
    first = next(
        p
        for day in payload["data"]["dailiesForAMonth"]
        for p in day["puzzles"]
        if p["urlPath"].endswith("/crossword")
    )
    first["urlPath"] = "bad/crossword"
    responses.post(API_URL, json=payload)
    client, _ = make_client()
    with pytest.raises(PuzzmoAPIError, match="malformed") as excinfo:
        client.fetch_month(2026, 10)
    assert "secret-token" not in str(excinfo.value)
