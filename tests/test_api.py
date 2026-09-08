import httpx
import pytest

from sleeper import api, ratelimit


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Route api.get through an httpx MockTransport; the limiter uses a temp state dir."""
    routes = {}

    def handler(request):
        status, body, headers = routes.get(request.url.path, (404, "", {}))
        return httpx.Response(status, content=body, headers=headers)

    clock = {"t": 1_000.0}

    def sleep(s):
        clock["t"] += s

    monkeypatch.setattr(api, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ratelimit, "_limiters", {
        h: ratelimit.RateLimiter(h, state_dir=tmp_path, now=lambda: clock["t"], sleep=sleep, notify=lambda m: None)
        for h in ("sleeper", "fantasypros")
    })
    return routes


def test_null_body_and_404_are_not_found(served):
    served["/v1/user/nobody"] = (200, "null", {})
    with pytest.raises(api.NotFound):
        api.get("sleeper", "user/nobody")
    with pytest.raises(api.NotFound):
        api.get("sleeper", "league/1")


def test_non_json_bodies_and_redirects_are_transient(served):
    served["/v1/state/nfl"] = (200, "<html>maintenance</html>", {"content-type": "text/html"})
    served["/v1/draft/1"] = (302, "", {"location": "https://elsewhere"})
    served["/v1/draft/2"] = (503, "", {})
    for path in ("state/nfl", "draft/1", "draft/2"):
        with pytest.raises(api.TransientError):
            api.get("sleeper", path)


def test_429_pauses_the_host_with_a_clamped_retry_after(served):
    served["/v1/a"] = (429, "", {"Retry-After": "86400"})
    served["/v1/b"] = (429, "", {"Retry-After": "inf"})
    served["/v1/c"] = (429, "", {})
    for path, expected in (("a", api.MAX_BLOCK), ("b", api.DEFAULT_BLOCK), ("c", api.DEFAULT_BLOCK)):
        with pytest.raises(api.RateLimited) as e:
            api.get("sleeper", path)
        assert e.value.seconds == expected
    assert ratelimit.limiter("sleeper").status()["paused_for_s"] > 0


def test_sleeper_root_shares_the_sleeper_limiter_and_repeats_list_params(served, monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "_client", httpx.Client(transport=httpx.MockTransport(lambda r: (seen.update(url=str(r.url)), httpx.Response(200, json=[]))[1])))
    params = {"season_type": "regular", "position[]": ["QB", "DEF"], "order_by": "adp_ppr"}
    assert api.get("sleeper_root", "projections/nfl/2026", params=params) == []
    assert seen["url"] == "https://api.sleeper.app/projections/nfl/2026?season_type=regular&position%5B%5D=QB&position%5B%5D=DEF&order_by=adp_ppr"
    assert ratelimit.limiter("sleeper").status()["sent_last_minute"] == 1
    assert "sleeper_root" not in ratelimit._limiters


def test_sleeper_root_429_pauses_the_sleeper_host(served):
    served["/projections/nfl/2026"] = (429, "", {"Retry-After": "30"})
    with pytest.raises(api.RateLimited) as e:
        api.get("sleeper_root", "projections/nfl/2026")
    assert e.value.host == "sleeper_root" and e.value.seconds == 30
    assert ratelimit.limiter("sleeper").status()["paused_for_s"] == 30


def test_fantasypros_requires_the_key_and_sends_it_as_a_header(served, monkeypatch):
    monkeypatch.delenv("FANTASYPROS_API_KEY", raising=False)
    with pytest.raises(api.SleeperError, match="FANTASYPROS_API_KEY"):
        api.get("fantasypros", "nfl/players")
    seen = {}
    monkeypatch.setenv("FANTASYPROS_API_KEY", "k-test")
    monkeypatch.setattr(api, "_client", httpx.Client(transport=httpx.MockTransport(lambda r: (seen.update(r.headers), httpx.Response(200, json={"players": []}))[1])))
    assert api.get("fantasypros", "nfl/players") == {"players": []}
    assert seen["x-api-key"] == "k-test"
