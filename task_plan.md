# Task Plan: Roadmap Implementation Slice

## Goal
Implement the first runnable slice of the 1M roadmap: a SQLite-backed queue for seed ingestion, resumable app/country jobs, policy document metadata, and stats.

## Phases
- [x] Phase 1: Scope the implementation slice
- [x] Phase 2: Add failing tests for queue schema and operations
- [x] Phase 3: Implement SQLite queue module and CLI
- [x] Phase 4: Wire docs and verification
- [x] Phase 5: Commit and push

## Key Questions
1. What is the smallest slice that materially advances the roadmap?
2. How can the existing JSONL worker be reused without a large rewrite?
3. Which checks prove the slice is usable?

## Decisions Made
- First slice: SQLite queue and ingestion/stats, not full distributed crawling.
- Keep existing collector script intact where possible; add queue-oriented modules around it.
- Use Python standard library SQLite for portability.
- Runtime data on this machine should live under `F:\ios-privacy-policy-collector\data`; do not commit fetched data or queue databases.

## Errors Encountered
- Windows SQLite temp cleanup failed with `WinError 32` while WAL mode was enabled. Removed default WAL mode from the reusable connection helper; a production runner can enable WAL explicitly for long-lived databases if needed.
- `with sqlite3.connect(...)` did not close connections on Windows; wrapped connections with `contextlib.closing` in code and tests.
- `contextlib.closing` removed implicit commit behavior from connection context managers; added explicit commits to write operations.

## Status
**Complete** - Queue store and worker pass tests; changes are ready to commit and push.
