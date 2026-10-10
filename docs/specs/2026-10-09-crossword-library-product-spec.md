# Crossword Library — Product Specification

**Status:** Draft for discussion  
**Date:** 2026-10-09  
**Scope:** Personal, self-hosted crossword acquisition, catalogue, and completion tracking

## 1. Summary

Build a small, self-hosted service that downloads and catalogues crossword puzzles and tracks **whether each puzzle has been completed**, independently of the application used to solve it. Initially support New York Times (NYT) and Puzzmo puzzles, a jailbroken Kindle Scribe running KOReader, and a computer-based crossword client. Existing solvers should be reused wherever practical; custom clients or upstream contributions can be developed later.

The product is a **crossword library and completion-status service**, not a crossword-solving engine. Think of it as a reading list for crossword puzzles.

## 2. Goals

- Automatically acquire puzzles from NYT and Puzzmo, including historical puzzles where feasible.
- Maintain a durable, deduplicated catalogue with stable puzzle identities.
- Track completion persistently across devices and clients.
- Offer a small API for existing and future crossword clients.
- Offer a minimal web UI to browse the queue, download files, and manually mark puzzles complete or incomplete.
- Support offline solving on the Scribe and reconcile completion later.
- Keep implementation, deployment, backup, and migration simple.

## 3. Non-goals (MVP)

- Synchronising partially filled grids, cursor position, elapsed time, or other solving progress.
- Building a new crossword solver or handwriting-recognition system.
- Automatically reflecting local completions back to NYT or Puzzmo accounts.
- Multi-user accounts, sharing, social features, leaderboards, or detailed statistics.
- Requiring a custom KOReader fork to use the first version.
- Supporting every crossword variant or every publisher immediately.

## 4. Agreed product principles

1. **Completion is the shared state.** Each solver can independently maintain its own in-progress state.
2. **The server is authoritative.** Client completion signals are imported into a permanent store; losing or truncating client history must not delete server history.
3. **Clients are replaceable.** Acquisition, catalogue, completion status, and solving UI remain separate.
4. **Manual override is essential.** A puzzle completed on a publisher website, on paper, or in an unsupported client can be marked complete manually.
5. **Imports are idempotent.** Re-importing a puzzle must not create duplicates or erase completion.
6. **Keep the MVP small.** Add capabilities based on actual usage rather than speculative framework design.

## 5. Initial system design

### Components

- **Downloader:** Run `xword-dl` on the server to retrieve NYT and Puzzmo puzzles, subject to verification of source support, authentication, historical access, and conversion fidelity.
- **Catalogue:** Store puzzle metadata, provenance, canonical identity, and locally available puzzle files.
- **Completion store:** SQLite database of completed puzzle IDs and timestamps.
- **API:** Small HTTP interface for listing puzzles, downloading files, and setting completion.
- **Web UI:** Minimal responsive catalogue and completion checklist, usable on a computer.
- **KOReader integration:** Initially use `omer-faruq/crossword.koplugin` and its existing `.puz` / `.ipuz` support. An optional file watcher or later upstream contribution reports completion to the server.
- **Filesystem transport:** Syncthing is a plausible way to distribute puzzle files and transfer local status files; direct HTTP is an alternative for future clients.

### Candidate technology choices (provisional, not locked)

- Python + FastAPI for the service.
- SQLite for metadata and completion records.
- Single Docker container and persistent volume for deployment.
- Daily scheduled acquisition job.
- API token for simple authentication.
- JSON export/import for portability and backup.

### Logical flow

```text
NYT / Puzzmo
    |
    v
 xword-dl ---> importer / ID normalization ---> catalogue + puzzle files
                                                |
                                         SQLite completion store
                                                |
                                         HTTP API + web UI
                                           /          \
                                    KOReader       desktop client
                                  (via adapter)    (future choice)
```

## 6. Data model

### Puzzle

Proposed fields:

- `id`: stable canonical identifier, e.g. `nyt/daily/2026-10-08`.
- `publisher`: `nyt`, `puzzmo`, etc.
- `series`: `daily`, `mini`, `crossword`, `big`, etc.
- `publication_date`: date, if available.
- `title`: human-readable title.
- `source_url` or `source_ref`: original provenance, if available.
- `file_format`, `file_path`: imported puzzle asset.
- `imported_at`: import timestamp.

The ID scheme is **proposed**, not final. It must distinguish variants and avoid depending on device-specific paths. An importer may maintain aliases for source IDs and filenames.

### Completion

Minimal logical record:

```json
{
  "puzzle_id": "nyt/daily/2026-10-08",
  "completed_at": "2026-10-09T09:30:00Z",
  "client": "koreader"
}
```

Absence of a completion record means **not marked complete**; it does not necessarily mean never started. `client` is optional provenance/debug metadata. Completion can be explicitly removed by the user.

For the MVP, completion is a boolean state with an optional timestamp, not a distinction between solved without help, checked, or finished with assistance.

## 7. Draft API (subject to refinement)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/puzzles` | List/filter catalogue, including completion status |
| `GET /api/puzzles/{id}` | Retrieve puzzle metadata and status |
| `GET /api/puzzles/{id}/file` | Download an available puzzle file |
| `PUT /api/puzzles/{id}/completion` | Mark a puzzle complete (idempotently) |
| `DELETE /api/puzzles/{id}/completion` | Mark a puzzle incomplete |
| `POST /api/import` | Import a file or initiate acquisition (exact contract TBD) |

**Implementation note:** IDs containing slashes need an explicit URL encoding/path-routing decision. An opaque URL-safe ID or query-based endpoint may be preferable. No endpoint shape is frozen yet.

The web UI should provide source/type/date filters, completed/uncompleted visibility, manual toggles, and file downloads. A calendar or backlog-oriented view would be useful but is not a hard MVP requirement.

## 8. KOReader findings and integration strategy

The preferred existing solver is [`omer-faruq/crossword.koplugin`](https://github.com/omer-faruq/crossword.koplugin). Research of its source found:

- `Crossword:saveCurrent(puzzle)` serializes the current puzzle, stores it in the plugin's settings, and updates a recent-entry record with `completed = puzzle:isSolved()`.
- The `Puzzle:isSolved()` method checks entered letters against the solution.
- Persistence uses KOReader's `LuaSettings`, stored in `DataStorage:getSettingsDir() .. "/crossword.lua"`.
- Recent entries include `source_type`, `source_ref`, `title`, `updated_at`, `progress_pct`, and `completed`.
- **Recent entries are capped at 30.** This is not a durable lifetime completion ledger.
- For locally imported puzzles, the plugin's save path does not appear to write progress back into the original `.puz` file; opening an old local-file recent entry reloads the source file.
- There is an `onPuzzleSolved(puzzle)` callback that could be extended in a small upstream PR.

Relevant source files:

- https://github.com/omer-faruq/crossword.koplugin/blob/main/main.lua
- https://github.com/omer-faruq/crossword.koplugin/blob/main/crossword_db.lua
- https://github.com/omer-faruq/crossword.koplugin/blob/main/crossword_puzzle.lua

### Integration options

**MVP option A — no plugin modifications:** Syncthing copies `crossword.lua` to the server; a watcher parses the `recents` table, maps source references to canonical puzzle IDs, and permanently records any `completed = true` entries. This is simple but has a theoretical missed-event risk if more than 30 puzzles cycle through before ingestion. Ensure copies are atomic/readable and that state is not overwritten by the server.

**Later option B — upstream contribution:** Have the plugin append a durable completion event (e.g. JSON Lines or another simple sidecar) when a puzzle is solved. The adapter imports events into the server. Prefer a small opt-in integration rather than maintaining a long-term fork.

Neither option requires synchronising grid state. The older `roygbyte/crossword.koplugin` has not shown a stronger persistence story in the research performed so far.

## 9. Acquisition and formats

- `xword-dl` is the leading candidate for fetching NYT and Puzzmo puzzles. It is an unofficial integration and may depend on publisher authentication and undocumented web behaviour.
- `.puz` is the practical first format for interoperability with existing clients.
- `.ipuz` is another useful supported interchange format; `.jpz` can be deferred.
- Store downloaded files and source metadata without modifying the originals unnecessarily.
- `.puz` can represent filled-in letters, but **the proposed service does not depend on embedded solving state**.
- Validate that NYT and Puzzmo puzzle variants, rebuses, metadata, and special mechanics survive conversion before declaring coverage.
- Respect publisher access requirements and avoid assuming a stable official NYT export API.

Candidate sources:

- https://github.com/thisisparker/xword-dl
- https://github.com/puzzmo-com/xd-crossword-tools

## 10. Desktop client

A desktop client should be able to load downloaded `.puz` puzzles, solve them with its own local state, and notify the service on completion. No desktop solver has been selected yet.

Candidates for evaluation include browser-based solvers such as XwordJS, Exolve, and self-hosted crossword projects. The first version can use the web UI to mark completion manually, so automatic desktop integration is not a prerequisite for launching the core service.

## 11. MVP acceptance criteria

1. A scheduled or manually invoked importer retrieves at least one supported puzzle from each configured source (once credentials/access are supplied).
2. Re-importing the same puzzle does not duplicate it or change its completion status.
3. A user can browse the combined queue and filter completed versus incomplete puzzles.
4. A user can download a puzzle file for use in a separate solver.
5. A user can mark a puzzle complete or incomplete from the web UI or API.
6. Completion persists across restarts, backups, and re-imports.
7. A KOReader completion can be ingested using an adapter once the source-to-canonical-ID mapping is configured and verified.
8. A completed puzzle appears completed in the shared catalogue regardless of which client reported it.
9. The service does not need to store or reconcile partially completed grids.
10. The catalogue and completion records can be backed up/exported without depending on a proprietary service.

## 12. Remaining open questions

### Highest priority — resolve before implementation

1. **Downloader behaviour:** Which NYT and Puzzmo variants does the current `xword-dl` version support? What authentication and historical-download flows actually work for the user's accounts?
2. **Stable identity:** How should canonical IDs handle publisher, series, date, special editions, and republished puzzles? How should local filenames and KOReader `source_ref` values map to those IDs?
3. **KOReader import path:** Can we reliably read `crossword.lua` through Syncthing, including its LuaSettings representation and update timing? Would an upstream completion-event PR be worthwhile immediately?
4. **Desktop player:** Which existing player is easiest to integrate with the completion API? Is manual completion marking enough for the first release?
5. **Source compatibility:** What information is lost converting Puzzmo and NYT variants into `.puz`? Are any puzzle types unsuitable for the initial release?

### Product and operational questions

6. Should a daily job download *all* available puzzle types or only a selected list? What backlog/history window should it initially populate?
7. Should the service automatically detect completions made on NYT/Puzzmo websites, or is manual marking sufficient? (Not an MVP requirement.)
8. Is Syncthing the preferred delivery method to KOReader, or would a lightweight direct-download integration be better?
9. Should a client be able to reverse a completion automatically, or should automatic ingestion be monotonic (only add completions) while manual actions can undo them? **Suggested default: monotonic ingestion, explicit manual undo.**
10. Should the service retain original source files alongside converted `.puz` files?
11. What are the deployment host, backup mechanism, and network exposure requirements? Is access restricted to a home network/VPN?
12. Should the schema be single-user only, or include a minimal future-proof user dimension? **Suggested default: single-user MVP.**
13. Should there be a machine-readable catalogue/export feed for clients (e.g. manifest JSON), in addition to the REST API?
14. How should a failed download, expired publisher session, or removed puzzle be surfaced in the UI?
15. Do we want optional links from a future Kindle morning briefing directly into the next uncompleted crossword? This is a later integration, not a core requirement.

## 13. Suggested implementation sequence

1. **Discovery spike:** Test `xword-dl` against one NYT and one Puzzmo puzzle; inspect actual downloaded metadata and variants. Validate KOReader settings and imported-file references with a real device sample.
2. **Core library:** Implement canonical IDs, catalogue, SQLite completion store, importer, and basic JSON API.
3. **Web queue:** Build the minimal browse/filter/mark/download UI.
4. **KOReader adapter:** Import completed entries from synced `crossword.lua`; preserve them beyond the plugin's 30-entry history.
5. **Desktop integration:** Select an existing solver and add completion reporting, or use manual marking until then.
6. **Optional upstream PR:** Add durable completion events and/or improved per-puzzle persistence to the KOReader plugin.
7. **Later enhancements:** Publisher-side completion import, handwriting input, richer catalogue feeds, and links from a Scribe morning briefing.

## 14. Explicitly deferred decisions

- Exact programming language/framework and Docker packaging details.
- API URL shapes and authentication mechanism.
- Which desktop solver to adopt.
- Whether KOReader integration is a watcher, a plugin PR, or both.
- Puzzle metadata schema beyond the minimum fields.
- Automated publisher-account completion reconciliation.

These should be resolved through short technical spikes, not by expanding MVP scope prematurely.
