import asyncio

from app.web_search import BraveSearchProvider, SearchRequest, TavilySearchProvider, search_web


class FakeResponse:
    def __init__(self, payload: dict, *, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_unconfigured_provider_is_not_exposed_as_available(monkeypatch) -> None:
    monkeypatch.setattr("app.web_search.settings.tavily_api_key", "")
    result = asyncio.run(search_web("query", provider="tavily", credentials={"tavily": ""}))
    assert result["success"] is False
    assert result["error_code"] == "search_provider_unconfigured"


def test_tavily_and_brave_normalize_results(monkeypatch) -> None:
    async def guarded(_client, method, _url, **_kwargs):
        if method == "POST":
            return FakeResponse({"request_id": "t-1", "results": [{"title": "T", "url": "https://example.com/t", "content": "snippet", "score": 0.9}]})
        return FakeResponse({"web": {"results": [{"title": "B", "url": "https://example.org/b", "description": "desc"}]}}, headers={"x-request-id": "b-1"})

    monkeypatch.setattr("app.web_search.guarded_request", guarded)
    tavily = asyncio.run(TavilySearchProvider("secret").search(SearchRequest("q")))
    brave = asyncio.run(BraveSearchProvider("secret").search(SearchRequest("q", time_range="week")))
    assert tavily.request_id == "t-1" and tavily.results[0].provider == "tavily"
    assert brave.request_id == "b-1" and brave.results[0].provider == "brave"
    assert tavily.results[0].url.startswith("https://")
