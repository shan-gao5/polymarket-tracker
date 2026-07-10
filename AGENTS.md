General Guidelines
Never use the em dash character "—". Use a plain dash "-" instead.
When writing commit messages, never automatically add the agent name as a co-author.
Never manually modify CHANGELOG.md files or files marked as auto-generated.
When writing or substantially editing long Markdown files, put each full sentence on its own line. Preserve normal Markdown structure, but avoid wrapping multiple sentences onto the same physical line.
When making technical decisions, do not give much weight to short-term development cost. Prefer quality, simplicity, robustness, scalability, and long-term maintainability.
When fixing a bug, always begin by reproducing it in an end-to-end setting that closely matches how an end user experiences it. This helps identify the actual problem so the fix addresses its root cause.
When performing end-to-end testing, be highly attentive to the visible user interface and overall user experience. If something clearly looks incorrect, even when it is not directly related to the current task, fix it when reasonably possible.
Apply the same high standard to engineering quality, including lint errors, test failures, type errors, build warnings, and flaky tests. If an issue is discovered, fix it even when it was not introduced by the current change.

## Project-specific knowledge

Gotchas and verified-against-the-live-API notes about the `polymarket-client` package, the `btc-updown-15m` market series, and this repo's SQLModel store live in the `polymarket-client` skill at `.claude/skills/polymarket-client/SKILL.md`, not here.
Load it before writing or debugging code that touches `polymarket`, realtime subscriptions, or `src/polytracker/store.py`.
Add new discoveries there, not to this file.
