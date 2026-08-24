from __future__ import annotations

import pytest


def test_provisioning_package_exports_expected_facade() -> None:
    from orchestune import provisioning

    assert hasattr(provisioning, "provision_issues")
    assert hasattr(provisioning, "main")
    assert hasattr(provisioning, "IssuePreview")
    assert hasattr(provisioning, "PlanMetadata")
    assert hasattr(provisioning, "ProvisionResult")
    assert callable(provisioning.provision_issues)
    assert callable(provisioning.main)


def test_provisioning_package_raises_attribute_error_for_unknown_symbol() -> None:
    from orchestune import provisioning

    with pytest.raises(AttributeError):
        _ = provisioning.non_existent_symbol
