# Migrations

`database/schema.sql` is the **v1 baseline**. Every later change goes in this
folder as `NNNN_short_description.sql`, starting at `0002`.

- Numbers must be contiguous; the runner refuses gaps.
- Each file is applied in one transaction and bumps `PRAGMA user_version`.
- Never edit a migration that has shipped — add a new one.
- Do not put `PRAGMA foreign_keys`, `BEGIN`, or `COMMIT` in a migration file;
  the runner manages those.
