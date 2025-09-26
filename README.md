
# Smartlead Tag Mapper v4

- Fixes state reset and Arrow conversion issues by using nullable Int64 with pd.NA, not 'n/a' strings in-memory.
- Exact case tag matching by default. Optional checkbox for case-insensitive matching.
- Progress bar for batch apply. Batch logs rendered as a table.
- Per-row results with status and error columns. Downloadable mapped and results CSVs.
