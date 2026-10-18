# Using the review window

The left-hand queue lists pull requests and security alerts. Planner order is a starting priority, not an approved merge sequence. Select an item to see its assessment and conversation on the right. Drag the divider to give either side more room.

## Selected item

The top area identifies the selected PR or alert, your saved decision, the agent's assessment and the next useful action. Expand **Details and evidence** for the reasoning. Investigation does not authorize a merge. When a PR is ready, **Review proposed merge** opens a separate exact confirmation; nothing merges just by opening it.

**Update a candidate PR branch**, when available, brings target-branch changes into that PR before validation. It does not merge the PR into its target or update your local checkout. Its approval is separate from merge approval.

**Conversation** contains the message box and the agent's access/usage information. **Send to agent** saves your message and starts or continues investigation; **Save without sending** only saves it. The latest exchange remains visible during new work and is labelled historical when appropriate. Expand the saved messages, answer history or technical evidence for more detail.

## Maintenance

This tab shows saved operations and follow-up work independently of the queue selection. Always check the repository and PR named in a result; selecting a different queue item does not change which operation that result describes.

- **Action outcome:** what happened to an approved operation. A saved success is not a new live check.
- **Post-merge verification:** remaining assessment or configured completion checks. Checking does not repeat the merge.
- **Local checkouts:** the current comparison, when checked, plus separately approved local-update controls. Previous updates are historical receipts, not proof that the checkout is current now. Repository instructions may require a different integration method that Watcher cannot execute.
- **PR branch updates:** recorded update outcomes and any recovery or validation continuation needed.

Remote merge, local synchronization, release/promotion and application-health verification are different steps. A completed merge does not imply the other steps happened. Follow the current guidance; do not repeat an already completed operation to advance a different step.

## Refresh and background work

| Control | What it does |
| --- | --- |
| Refresh saved queue | Reloads the saved queue and rechecks checkout/follow-up status, which can include read-only GitHub checks. Does not rebuild recommendations or start an agent. |
| Collect fresh recommendations | Reads current GitHub PRs, alerts and checks and rebuilds the queue. Does not itself start an investigation or merge. |
| Refresh saved agent replies | Reloads already saved replies; does not ask the agent a new question. |
| Hide to tray | Hides the window while its current work can continue. |
| Exit | Requests shutdown; ongoing approved operations may need to finish and record their outcomes first. It does not disable a separately installed schedule. |

Pending **Agent access requests** open a dedicated approval view. **Allow once** applies to one exact request; batch approval applies only to the displayed requests, not future commands or a merge. Activity and elapsed time indicate that work is ongoing, not proof that a particular tool is progressing. **Recent action results** below the workspace retains saved outcomes and recovery information.
