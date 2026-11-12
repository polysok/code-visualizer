import pytest

from code_visualizer.utils.path_sandbox import is_safe_download_url


@pytest.mark.parametrize("host", ["gitlab.com", "raw.gitlab.com", "GITLAB.COM"])
def test_gitlab_https_bundle_urls_are_allowed(host):
    assert is_safe_download_url(f"https://{host}/group/project/-/raw/main/graph.cviz")


@pytest.mark.parametrize("url", [
    "http://gitlab.com/group/graph.cviz",
    "https://gitlab.com.evil.invalid/graph.cviz",
    "https://evilgitlab.com/graph.cviz",
    "https://gitlab.com@evil.invalid/graph.cviz",
    "https://127.0.0.1/graph.cviz",
])
def test_gitlab_support_keeps_scheme_and_hostname_boundaries(url):
    assert not is_safe_download_url(url)
