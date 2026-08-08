---
schema_version: 1
kind: upload-assistant.site-rule.v1
site:
  code: TTG
  display_name: Local verification fixture
  roles: [source]
source:
  url: https://example.invalid/upload-assistant/local-verification-rule
  captured_at: "2026-08-08"
  complete: true
  scope: Offline fixture used only to verify the local rule revision lifecycle.
  text_sha256: ""
automation:
  manual_review_required: true
  download: false
  upload: false
  retorrent: false
  auto_pull: false
  auto_upload: false
limits: {}
seeding: {}
transfer:
  freeleech_required: false
  forbid_original_torrent: true
  preserve_content: true
obligations:
  - id: local-verification-no-external-action
    scope: local_verification
    verification: programmatic
    blocking: true
    resolution: enforced
    description: This fixture must never authorize an external tracker action.
    evidence_refs: [local-compose-verifier]
    enforcement: All automation capabilities and executable switches are disabled.
notes:
  - This is synthetic public test data, not a tracker rule or operator approval.
review:
  status: draft
---

# Original rule fixture

This synthetic document exists only to test immutable import, exact-fingerprint approval,
activation, listing, and reading through the native Go CLI against isolated PostgreSQL.
It contains no tracker credentials and grants no permission for an external action.
