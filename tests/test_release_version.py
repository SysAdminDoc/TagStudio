# Copyright (C) 2025
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


from unittest.mock import Mock

import requests

from tagstudio.core.ts_core import TagStudioCore


def test_latest_release_version_returns_valid_tag(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"tag_name": "v9.5.7"}
    request = Mock(return_value=response)
    monkeypatch.setattr("tagstudio.core.ts_core.requests.get", request)
    TagStudioCore.get_most_recent_release_version.cache_clear()

    assert TagStudioCore.get_most_recent_release_version() == "9.5.7"
    request.assert_called_once_with(
        "https://api.github.com/repos/TagStudioDev/TagStudio/releases/latest", timeout=3
    )


def test_latest_release_version_is_optional_when_network_is_unavailable(monkeypatch):
    request = Mock(side_effect=requests.RequestException("offline"))
    monkeypatch.setattr("tagstudio.core.ts_core.requests.get", request)
    TagStudioCore.get_most_recent_release_version.cache_clear()

    assert TagStudioCore.get_most_recent_release_version() is None
