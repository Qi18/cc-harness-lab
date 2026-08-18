---
name: code-review
description: Review changed code for correctness, regressions, security, and missing tests.
---

# Code Review

Review the requested change without modifying files unless the user separately asks for fixes.

1. Read the task and inspect the relevant diff before exploring unrelated files.
2. Trace changed inputs through their callers and observable outputs.
3. Look for correctness bugs, regressions, unsafe behavior, and missing error handling.
4. Check whether tests cover the changed behavior and important failure paths.
5. Run the smallest relevant checks when execution is available.
6. Report actionable findings first, ordered by severity, with precise file and line references.
7. If no defect is found, say so explicitly and mention any remaining validation gap.
