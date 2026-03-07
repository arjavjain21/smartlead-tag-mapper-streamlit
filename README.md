# Smartlead Tag Mapper v7

- Supports overwrite mode to remove existing tags on targeted accounts before applying CSV tags.
- Requires explicit overwrite confirmation and shows pre-apply impacted account count.
- Two-phase execution visibility: delete phase (overwrite) + apply phase.
- Deduplicates account-tag apply intents for better API efficiency.
- Preserves dry-run support with simulated statuses.
- Accepts either email or domain input values; when domains are provided the app tags every inbox matching that domain.

- Supports comma-separated tags in a single CSV cell (e.g., `00IKT,BACKUP`).
- Overwrite delete phase removes mappings by targeting selected accounts against all known workspace tag IDs.
