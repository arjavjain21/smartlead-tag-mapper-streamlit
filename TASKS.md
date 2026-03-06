# Overwrite Existing Tags Feature Tasks

This task list captures the agreed behavior for adding **Overwrite existing tags** to the Smartlead Tag Mapper app.

## Scope Summary
When enabled and confirmed, overwrite mode should:
1. Remove all existing tags on target accounts.
2. Proceed to apply CSV-provided tags.
3. Show clear phase-wise results for delete + apply.

---

## Phase 1 — UX and Controls

- [ ] Add an `Overwrite existing tags` toggle in the Apply section.
- [ ] Add explicit confirmation prompt/gate:
  - [ ] Warning text: enabling overwrite will remove existing tags from accounts matched from CSV and then re-apply CSV tags.
  - [ ] Require affirmative confirmation before execution.
- [ ] Show pre-apply overwrite impact count:
  - [ ] `N accounts will be overwritten.`
- [ ] Keep existing dry-run flow, but include overwrite simulation messaging.

## Phase 2 — Data Preparation and Dedup

- [ ] Build unique target account list from mapped rows (`email_account_id` non-null).
- [ ] Deduplicate account IDs before deletion.
- [ ] Deduplicate apply intents (account_id + tag_id pairs) before apply for efficiency.
- [ ] Preserve current domain expansion behavior (domains map to all matching inboxes).

## Phase 3 — Fetch Existing Tag Mappings

- [ ] Extend data fetching to get existing account↔tag mappings for target accounts.
- [ ] Prefer GraphQL query shape that includes account IDs and associated tag IDs.
- [ ] Validate schema/permissions for production token scopes.
- [ ] Cache mapping fetch similarly to existing cached fetches if safe.

### Notes
- Existing tags query is already known and can be reused as needed for tag metadata:
  - `query GetTags { tags { id name created_at updated_at } }`

## Phase 4 — Delete Phase (Overwrite)

- [ ] Add DELETE API helper for `/v1/email-accounts/tag-mapping` using `api_key` query param.
- [ ] Use batch size `25` for deletion requests.
- [ ] Remove all existing tags for each targeted account.
- [ ] Treat non-existing mappings as idempotent skips.
- [ ] Continue execution on partial failures, but record failures clearly.

## Phase 5 — Apply Phase (Existing + Enhancements)

- [ ] Run add/apply phase after delete phase completes.
- [ ] Preserve existing batch size `25` and progress handling.
- [ ] Ensure apply still works when overwrite is off (no behavior regression).

## Phase 6 — Results, Logs, and Summaries

- [ ] Explicitly display **two phases** in UI/results:
  - [ ] Phase A: Delete existing tags (or simulated delete in dry run).
  - [ ] Phase B: Apply CSV tags (or simulated apply in dry run).
- [ ] Add per-operation logs with clear status values (`deleted`, `skipped`, `failed`, `applied`).
- [ ] Mark failed deletions clearly with reason/error.
- [ ] Include summary counters per phase and total.
- [ ] Ensure downloadable results include phase/action columns.

## Phase 7 — Dry Run Behavior

- [ ] In dry run + overwrite on, do not mutate APIs.
- [ ] Show simulated delete + apply counts and expected actions.
- [ ] Keep status labels distinct from real execution (e.g., `SIMULATED_*`).

## Phase 8 — Validation and QA Checklist

- [ ] Overwrite OFF path remains unchanged from current behavior.
- [ ] Overwrite ON requires confirmation.
- [ ] Correct count shown for `N accounts will be overwritten`.
- [ ] Dedup works (no duplicate delete/apply calls for same pair).
- [ ] Delete skips are non-fatal and logged.
- [ ] Partial delete failures are visible, and apply still proceeds.
- [ ] Dry run accurately reflects two-phase simulation.
- [ ] CSV downloads include new status/details.

## Implementation Caveats to Track

- [ ] Confirm exact API limits and response shape for DELETE endpoint in production.
- [ ] Confirm best source for current account-tag mappings (GraphQL or REST listing endpoint).
- [ ] Ensure robust handling when mapping fetch fails (safe abort with actionable error).
- [ ] Avoid excessive API calls by deduping and batching aggressively.
