---
description: Run swarm orchestration tests on a dedicated port with auto-shutdown
---

Run the swarm test suite and summarize the results.

## Steps

1. Run the test runner (adjust timeout as needed):

```bash
uv run swarm test --timeout 300
```

This will:
- Launch a synthetic test project on port 9091
- Monitor worker activity with `[STATE]`, `[TASK]`, `[DRONE]`, `[QUEEN]` prefixed console output
- Auto-shutdown when all tasks complete (or on timeout)
- Generate a markdown report

2. Watch the console output for progress indicators:
   - `[STATE]` — worker state transitions
   - `[TASK]` — task assignments and board updates
   - `[DRONE]` — drone decisions (auto-approve, escalate, etc.)
   - `[QUEEN]` — queen analysis results
   - `[HIVE]` — hive lifecycle events (complete, workers changed)
   - `[ESCALATE]` — escalation warnings

3. When the test completes, read the report file path from the output.

4. Read the report and summarize:
   - Exit code (0=success, 1=failure, 2=timeout)
   - Number of tasks completed vs total
   - Key metrics (drone decisions, queen calls, state transitions)
   - Any notable observations or failures
   - AI analysis highlights (if present in the report)

## Options

- `--port N` — use a different port (default: 9091 from config)
- `--timeout N` — seconds before auto-shutdown (default: 300)
- `--no-cleanup` — keep the tmux session and temp dir for debugging
- `-c CONFIG` — use a specific swarm.yaml
