"""Hosted CLI preparation retains existing dataclass framework semantics."""

from smartmemory_app.hosted_code import prepare_code_index


def test_hosted_cli_framework_bundle(tmp_path):
    root = tmp_path / "test_fw_checkout"
    root.mkdir()
    (root / "view.test.tsx").write_text(
        "import { test } from 'vitest';\nexport const View = () => <span/>;\nfunction useValue() { return 1; }\ntest('uses hook', () => useValue());\n"
    )
    try:
        bundle, result = prepare_code_index(
            str(root), "test_fw_repo", "test_fw_commit", None, ["typescript"]
        )
        assert result.files_parsed == 1
        entities = {e["name"]: e for e in bundle["entities"]}
        assert (
            entities["View"]["entity_type"] == "component"
            and entities["View"]["is_exported"]
        )
        assert entities["useValue"]["entity_type"] == "hook"
        tests = [e for e in bundle["entities"] if e["entity_type"] == "test"]
        assert tests[0]["test_evidence"]
        assert any(
            e["relation_type"] == "TESTS" and e["properties"]["candidates"]
            for e in bundle["relations"]
        )
    finally:
        import shutil

        shutil.rmtree(root)
