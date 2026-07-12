import os

import yaml


def test_ci_workflow_has_explicit_permissions():
    ci_workflow_path = os.path.join(
        os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml"
    )
    assert os.path.exists(ci_workflow_path), f"{ci_workflow_path} does not exist"

    with open(ci_workflow_path, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    # ワークフローに permissions キーが存在することを確認
    assert (
        "permissions" in workflow
    ), "permissions block is missing in .github/workflows/ci.yml"

    # permissions が辞書型であることを確認
    permissions = workflow["permissions"]
    assert isinstance(permissions, dict), "permissions block must be a dictionary"

    # permissions に contents: read が含まれていることを確認
    assert (
        permissions.get("contents") == "read"
    ), "permissions.contents must be 'read' to restrict default token scope"
