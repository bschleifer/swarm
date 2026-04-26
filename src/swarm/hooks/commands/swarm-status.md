---
description: Show your current Swarm task, queue, peer worker status, and any unread messages.
---

Show me a tight Swarm status summary using the coordination tools.

1. Call `mcp__swarm__swarm_task_status` with `filter="mine"` to see my assigned tasks.
2. Call `mcp__swarm__swarm_check_messages` to see unread messages.
3. Call `mcp__swarm__swarm_task_status` with `filter="all"` to see overall board state.

Then summarize in 5–10 lines covering:
- My current task (if any) — title + status + percent if known
- My queue (assigned but not yet IN_PROGRESS)
- Unread messages — count + senders
- Notable peer activity (other workers' current tasks)

No headers; bullets are fine. Keep it under ~150 words.
