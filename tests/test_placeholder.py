from orchestune.version import get_version


def test_get_version_returns_semver_string():
    assert get_version() == "0.1.0"
