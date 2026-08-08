from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ptcli.config import load_config
from src.ptcli.policies import build_site_policy, build_site_policy_config_audit
from src.ptcli.service import ServiceError, openapi_payload, site_rule_document_review_payload, site_rule_document_validate_payload
from src.ptcli.site_rule_docs import (
    SITE_POLICY_SNAPSHOT_KIND,
    approve_site_rule_document,
    compile_site_policy_snapshot,
    inspect_site_rule_documents,
    load_site_policy_snapshot,
    render_site_rule_document,
    validate_site_rule_document,
)


def _metadata(*, tracker: str = "U2", complete: bool = True, resolution: str = "accepted") -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ptcli.site_rule_document.v1",
        "tracker": tracker,
        "display_name": tracker,
        "roles": ["source"],
        "rules_url": "https://u2.dmhy.org/rules.php",
        "captured_at": "2026-08-08",
        "source_complete": complete,
        "source_scope": "test fixture",
        "source_text_sha256": "",
        "review_status": "draft",
        "reviewer": "",
        "reviewed_at": "",
        "review_fingerprint": "",
        "notes": ["local conservative policy"],
        "automation": {"manual_review_required": True, "download": True, "upload": False, "retorrent": True},
        "qbit_limits": {"download_limit": "20MiB/s"},
        "seeding_requirements": {"min_seed_time_hours": 72},
        "transfer_rules": {
            "freeleech_required": False,
            "required_promotions": [],
            "forbidden_title_patterns": [],
            "forbidden_release_groups": [],
        },
        "obligations": [
            {
                "id": "u2-manual-rule",
                "scope": "source_download",
                "verification": "manual",
                "blocking": True,
                "resolution": resolution,
                "description": "A human checked the source rule.",
                "evidence_refs": ["rule 1"],
                "enforcement": "Stop when the rule cannot be confirmed.",
            }
        ],
    }


def _write_document(path: Path, *, complete: bool = True, resolution: str = "accepted") -> None:
    body = "# 原始规则\n\nDo not upload the private source torrent directly to another tracker.\n\n# 结构化说明\n\nFixture.\n"
    path.write_text(render_site_rule_document(_metadata(complete=complete, resolution=resolution), body), encoding="utf-8")


def _approve(path: Path) -> dict[str, object]:
    return approve_site_rule_document(
        path,
        reviewer="tester",
        reviewed_at="2026-08-08T12:00:00+08:00",
        write=True,
    )


def test_draft_is_readable_but_incomplete_source_and_pending_obligation_cannot_be_approved(tmp_path: Path) -> None:
    path = tmp_path / "U2.md"
    _write_document(path, complete=False, resolution="pending")

    validation = validate_site_rule_document(path)
    review = _approve(path)

    assert validation["valid"] is True
    assert validation["ready_for_compile"] is False
    assert review["ok"] is False
    assert review["written"] is False
    assert path.read_text(encoding="utf-8").startswith("+++\nschema_version = 1")
    assert any("source_complete=true" in blocker for blocker in review["blockers"])
    assert any("blocking obligations" in blocker for blocker in review["blockers"])


def test_approved_document_compiles_reproducible_snapshot_and_runtime_loads_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rules_dir = tmp_path / "site-rules"
    rules_dir.mkdir()
    path = rules_dir / "U2.md"
    _write_document(path)

    review = _approve(path)
    snapshot_path = tmp_path / "site-policies.generated.json"
    first = compile_site_policy_snapshot(rules_dir, output_path=snapshot_path, write=True)
    second = compile_site_policy_snapshot(rules_dir, output_path=snapshot_path, write=False)

    assert review["ok"] is True
    assert review["written"] is True
    assert first["written"] is True
    assert first["snapshot"] == second["snapshot"]
    loaded_snapshot = load_site_policy_snapshot(snapshot_path)
    assert loaded_snapshot["kind"] == SITE_POLICY_SNAPSHOT_KIND
    assert loaded_snapshot["policies"]["U2"]["qbit_limits"]["download_limit"] == "20MiB/s"

    config_path = tmp_path / "config.py"
    config_path.write_text("config = {'DEFAULT': {}, 'TORRENT_CLIENTS': {}}\n", encoding="utf-8")
    monkeypatch.setenv("PTCLI_SITE_POLICY_SNAPSHOT", str(snapshot_path))
    config = load_config(str(config_path))
    policy = build_site_policy(config, "U2")
    audit = build_site_policy_config_audit(config, policy, roles=["source"], accept_rules=True)

    assert policy.download_rate_limit == 20 * 1024 * 1024
    assert audit["policy_source"] == "markdown_snapshot"
    assert audit["policy_document"]["reviewer"] == "tester"
    assert audit["policy_snapshot"]["snapshot_sha256"] == loaded_snapshot["snapshot_sha256"]


def test_source_text_tampering_invalidates_approved_document(tmp_path: Path) -> None:
    path = tmp_path / "U2.md"
    _write_document(path)
    assert _approve(path)["ok"] is True

    path.write_text(path.read_text(encoding="utf-8").replace("private source torrent", "changed source torrent"), encoding="utf-8")
    validation = validate_site_rule_document(path)

    assert validation["valid"] is False
    assert validation["ready_for_compile"] is False
    assert validation["source_text_sha256"]["matches"] is False
    assert validation["review_fingerprint"]["matches"] is False


def test_compile_never_writes_partial_snapshot(tmp_path: Path) -> None:
    rules_dir = tmp_path / "site-rules"
    rules_dir.mkdir()
    approved = rules_dir / "U2.md"
    _write_document(approved)
    assert _approve(approved)["ok"] is True
    draft = rules_dir / "CHD.md"
    metadata = _metadata(tracker="CHD")
    metadata["rules_url"] = "https://ptchdbits.co/rules.php"
    draft.write_text(render_site_rule_document(metadata, "# 原始规则\n\nCHD draft.\n"), encoding="utf-8")
    output = tmp_path / "snapshot.json"

    result = compile_site_policy_snapshot(rules_dir, output_path=output, write=True)

    assert result["ok"] is False
    assert result["written"] is False
    assert result["snapshot"] is None
    assert output.exists() is False


def test_list_and_http_review_keep_explicit_confirmation_and_path_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rules_dir = tmp_path / "site-rules"
    rules_dir.mkdir()
    _write_document(rules_dir / "U2.md")
    monkeypatch.setenv("PTCLI_SITE_RULES_DIR", str(rules_dir))

    listing = inspect_site_rule_documents(rules_dir)
    blocked_review = site_rule_document_review_payload({"rules_dir": str(rules_dir), "tracker": "U2"})

    assert listing["count"] == 1
    assert listing["ready_count"] == 0
    assert blocked_review["status"] == "blocked"
    assert blocked_review["written"] is False
    with pytest.raises(ServiceError, match="inside rules_dir"):
        site_rule_document_validate_payload({"rules_dir": str(rules_dir), "file": "../outside.md"})


def test_openapi_describes_site_rule_document_endpoints() -> None:
    paths = openapi_payload()["paths"]

    assert "get" in paths["/v1/site-rule-documents"]
    assert "post" in paths["/v1/site-rule-documents/validate"]
    assert "post" in paths["/v1/site-rule-documents/review"]
    assert "post" in paths["/v1/site-rule-documents/compile"]


def test_snapshot_hash_tampering_fails_closed(tmp_path: Path) -> None:
    rules_dir = tmp_path / "site-rules"
    rules_dir.mkdir()
    path = rules_dir / "U2.md"
    _write_document(path)
    assert _approve(path)["ok"] is True
    snapshot_path = tmp_path / "snapshot.json"
    assert compile_site_policy_snapshot(rules_dir, output_path=snapshot_path, write=True)["written"] is True
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["policies"]["U2"]["qbit_limits"]["download_limit"] = "unlimited"
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_site_policy_snapshot(snapshot_path)
