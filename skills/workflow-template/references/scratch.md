# Session Scratch Directory

Create one repository-local, Git-ignored session directory before writing any
plan or CLI body file:

```text
.orchestune/tmp/<artifact>-<issue-or-task>-<UTC timestamp>-<random>/
```

Use UTC `YYYYMMDDTHHMMSSZ` and a UUID or equivalent unpredictable `<random>`
component. Use the Issue number when available, otherwise a short task slug. Reuse
that directory throughout the workflow and keep `implementation-plan.md`,
`pr-body.md`, `review-reply.md`, and other scratch artifacts inside it. Never use a
fixed repository-root file or the OS-global `/tmp`. Ensure the target project's
`.gitignore` contains `.orchestune/tmp/` before using this template.
