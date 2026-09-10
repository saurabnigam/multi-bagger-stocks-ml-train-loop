window.QUANT_KB = {
  "decisions": [
    {
      "decision_id": "D-0000-migration",
      "proposal_id": null,
      "kind": "data_fix",
      "tier": 0,
      "subject_id": "legacy_migration",
      "title": "Legacy database migration and defect logging",
      "context": "Migration of historical 2026 legacy snapshots and model active weights into V2 schema with defect tracking.",
      "options_json": "[\"migrate_read_only\", \"discard_history\"]",
      "decision": "Migrate legacy snapshots with defect flags into V2 tables without modifying source.",
      "evidence_refs_json": "[\"quant_engine.db\"]",
      "criteria_check_json": "{}",
      "decided_on": "2026-09-03",
      "decided_by": "system:migration",
      "approver_kind": "system",
      "ratified_by": null,
      "ratified_on": null,
      "status": "applied",
      "effective_from": "2026-06-14",
      "applied_on": "2026-09-10T08:27:20.886042Z",
      "adr_path": "/Users/saurabhnigam/Desktop/Projects/mb-review-fixes/knowledge/decisions/ADR-D-0000-migration.md",
      "supersedes": null,
      "reverted_by": null,
      "git_sha": "git_sha"
    }
  ],
  "proposals": [],
  "lessons": [],
  "hypotheses": []
};
