# /md — Markdown document helper

Write or update a markdown document for this project.

## What to do

The user will pass a document type and optional details as arguments (e.g. `/md changelog v1.3.0` or `/md session` or `/md release`).

Interpret the argument and take the most useful action:

### `changelog` (+ optional version)
Add a new version block to `changelog.md` at the top (below the header, above the previous version). Pull the version from git tags if not specified. Summarise changes from `git log` since the last tag. Follow the existing format: `## [version] — YYYY-MM-DD`, then `### Added`, `### Changed`, `### Fixed`, `### Tests` sections as needed.

### `session` (+ optional date)
Create a session log file named `SESSION_YYYY-MM-DD.md` in the project root. Summarise what was done in the current conversation: items completed, files changed, test counts before/after, any issues encountered. Use today's date if not specified.

### `release` (+ optional version)
Create or update `RELEASE_NOTES.md`. Summarise user-facing changes for the specified version in plain language (no internal jargon). Include: what changed, why it matters, any upgrade steps needed.

### `debt` or `tech-debt`
Open `TECHNICAL_DEBT.md` and add a new entry for something identified in the current conversation. Each entry: title, description, effort estimate (S/M/L), priority (P1–P3), and date added.

### `readme` (section name)
Update a specific section of `README.md` — e.g. `/md readme architecture` regenerates the Architecture section based on current code structure.

### Any other argument
Treat it as a free-form document title. Create a new `.md` file in the `docs/` folder named after the argument (snake_cased). Write a well-structured document relevant to this project.

## Rules
- Always read the existing file before writing to avoid clobbering content.
- Use `git log` and `git diff` to ground changelog/session entries in actual changes rather than guessing.
- Keep language direct and technical — this project's audience is the developer, not end users.
- Do not add filler sections. If a section has nothing to say, omit it.
- Commit the file after writing if the user has previously been committing changes in this session.
