import httpx
import pytest
import respx

from jarvis.integrations.base import ProviderError
from jarvis.integrations.github import GitHubAdapter

API = "https://api.github.com"


@pytest.fixture
def gh():
    with httpx.Client() as c:
        yield GitHubAdapter("ghp_secret", client=c)


@respx.mock
def test_repo_status_excludes_prs_from_issues(gh):
    repo = respx.get(f"{API}/repos/aj/jarvis").mock(return_value=httpx.Response(200, json={
        "full_name": "aj/jarvis", "html_url": "https://github.com/aj/jarvis", "default_branch": "main",
        "pushed_at": "2026-09-27T00:00:00Z"}))
    issues = respx.get(f"{API}/repos/aj/jarvis/issues").mock(return_value=httpx.Response(200, json=[
        {"number": 1, "title": "Bug", "html_url": "https://github.com/aj/jarvis/issues/1", "labels": [{"name": "bug"}]},
        {"number": 2, "title": "PR", "html_url": "u", "pull_request": {}}]))
    respx.get(f"{API}/repos/aj/jarvis/pulls").mock(return_value=httpx.Response(200, json=[
        {"number": 2, "title": "PR", "html_url": "https://github.com/aj/jarvis/pull/2", "draft": True,
         "head": {"ref": "feat"}}]))
    s = gh.repo_status("aj/jarvis")
    assert [i["number"] for i in s["open_issues"]] == [1] and s["open_issues"][0]["labels"] == ["bug"]
    assert s["open_prs"][0]["url"].endswith("/pull/2") and s["open_prs"][0]["draft"] is True
    assert s["default_branch"] == "main" and s["url"] == "https://github.com/aj/jarvis"
    h = repo.calls[0].request.headers
    assert h["X-GitHub-Api-Version"] == "2022-11-28" and h["Authorization"] == "Bearer ghp_secret"
    assert issues.calls[0].request.url.params["per_page"] == "20"
    assert issues.calls[0].request.url.params["state"] == "open"


@respx.mock
@pytest.mark.parametrize("status,headers,kind", [
    (401, {}, "auth"), (403, {}, "forbidden"), (404, {}, "not_found"),
    (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}, "rate_limit"),
    (429, {"retry-after": "30"}, "rate_limit")])
def test_errors(gh, status, headers, kind):
    respx.get(f"{API}/repos/aj/jarvis").mock(return_value=httpx.Response(status, headers=headers,
                                                                         json={"message": "m"}))
    with pytest.raises(ProviderError) as e:
        gh.repo_status("aj/jarvis")
    assert e.value.kind == kind


def test_invalid_repo_name(gh):
    with pytest.raises(ProviderError):
        gh.repo_status("../etc")
    assert "ghp_secret" not in repr(gh)
