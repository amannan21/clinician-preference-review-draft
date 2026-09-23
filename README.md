# Clinician preference review (draft)

Draft development snapshot. This public repository contains source code only. Clinical datasets, exported workbooks, review history and credentials are excluded from this repository. The app requires an authorized local `data/examples.json` bundle before startup. It is not a hosted service.

Use Python 3.11+ and Node.js 18+ for the optional UI checks. Install the Python dependencies and start the local server:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Open http://127.0.0.1:8766. The connection form accepts a **LangSmith De-ID** API key and an optional workspace UUID. The key stays in server memory and is cleared by disconnect/restart. The destination is `clinician-preference-review-100`; authenticated dataset listing verifies access. Connecting does not create the dataset or send examples. The first explicit **Approve & send** creates it if missing; an incompatible existing dataset is rejected.

The deck contains 100 five-encounter examples: seven patient-linked and three random sets for each of the ten selected specialties. There are 20 additional, unused substitution sets: one per specialty/grouping combination. A replacement preserves the card's specialty/grouping and local slot identity. Its notes and reference learning replace the current set; previous contents remain in local history. After a reserve is used, that grouping's replacement button is disabled. Sending a replacement updates the same remote example rather than appending another card.

Each card has five original generated/edited note pairs, exact source versions and hashes, source split and date metadata, sampling method, source-author caveats, and an editable reference learning. Evidence buttons jump to the referenced summary. Generated summaries missing from the sparse edited source are **unobserved**, not deletions. Word-level highlights ignore whitespace while preserving the original strings. Oversized diffs display an explicit highlighting limitation.

Patient-linked sets use all source splits, as authorized for exploration. Random sets use `test`. Patient IDs are distinct across the 100 sets, and all 500 encounters are distinct. Same-patient continuity does **not** establish clinician identity. Source snapshots start at July 24, 2026; file timestamps identify deidentification versions, not clinical chronology. Cases within patient sets are ordered by deidentified appointment time. The final neurology random substitute is sampled from September 14 onward. Sampling uses seeded selection and edit/shape/date screening; replenishment prioritizes patients with at least seven source records. These are curated exploratory fixtures, not an unbiased sample or a leakage-free benchmark.

At the user's request, the deck was rebalanced to **50 preference / 50 abstention** reference learnings. Twenty-three abstention sets were replaced within their exact specialty/grouping categories; the original 27 positive sets were preserved. Seven replacements came from already audited unused reserves and sixteen from newly sampled sets. The same learning prompt and evidence audit were used throughout. Replacements record `outcome_enrichment` in metadata: this deliberately selected class balance is not an estimate of the natural frequency of clinician preferences. Prior cards remain in local history, and consumed reserves were replenished so all 20 specialty/grouping combinations still have a substitute. Rebalancing does not upload or revise anything in LangSmith.

Reference learnings are model drafts generated through Bilrost's approved model gateway. They reuse the reflection agent's rules and add explicit sparse-edit, attribution, and evidence constraints. A proposed preference must cite at least two encounters and pass a second model evidence audit. Abstention is valid when a durable writing preference is unsupported. Every saved learning includes its model, prompt hash, provenance and evidence references; these are review targets, not independently established gold labels. Human edits are validated against the set's source references. Original notes cannot be edited in the UI.

**Save revision locally** persists learning changes. **Approve & send** saves locally first, then writes only the current card and reads it back to verify notes, learning and revision. Stable dataset-scoped UUIDs prevent duplicate examples on retry. A network failure leaves the local revision intact. Notes are inputs; learning, evidence, provenance and human review status are metadata. No answer is injected into the model inputs. There are no automatic bulk uploads.

Local source bundles and the SQLite review/history database are in `data/` (ignored by Git, restrictive permissions). They contain deidentified clinical data. The server binds only to loopback, rejects foreign hosts/origins, requires a per-process token for writes, serves no remote scripts and logs no note bodies. It is a single-user local app; do not expose it on a shared/public interface.

Checks:

```sh
node test_status.cjs
python test_review.py
node check_ui.cjs  # Requires Playwright with Chrome, the running app and an authorized local dataset
```

`prepare.py` performs bounded read-only BigQuery selection; `learn.py` fills missing reference learnings without replacing completed ones. Neither script publishes to LangSmith. The review server refreshes untouched draft cards as the initial learning generation completes. Existing human revisions are retained.

Data preparation requires authorized BigQuery CLI access to the source table. Learning generation requires approved gateway access; `LLM_GATEWAY_API_KEY` is read from the environment. Neither credential belongs in this repository. A bundle contains `examples` and `substitutes` arrays; each example has `id`, `revision`, `status`, `metadata`, and five `cases` with `source` and `source_note_sha256`. The source shape is demonstrated with synthetic text in `test_review.py`.
