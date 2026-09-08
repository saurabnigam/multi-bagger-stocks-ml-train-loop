# ADR: Legacy database migration and defect logging (D-0000-migration)

- **Decision ID:** D-0000-migration
- **Date:** 2026-09-03
- **Tier:** Tier 0
- **Kind:** data_fix
- **Subject ID:** legacy_migration
- **Approver Kind:** system
- **Decided By:** system:migration
- **Status:** applied
- **Effective From:** 2026-06-14
- **Ratified By:** N/A
- **Ratified On:** N/A
- **Git SHA:** git_sha

## Context
Migration of historical 2026 legacy snapshots and model active weights into V2 schema with defect tracking.

## Options Considered
["migrate_read_only", "discard_history"]

## Decision
Migrate legacy snapshots with defect flags into V2 tables without modifying source.

## Evidence References
["quant_engine.db"]

## Criteria Check
{}
