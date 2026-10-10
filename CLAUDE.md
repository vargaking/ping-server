# Working on this repo

## Tickets (Linear: team Zeta Chat, project Web Alpha)
- Linear is the source of truth. A dated "Update <date>" section in a ticket overrides older text.
- Work one ticket at a time. One agent writes at a time; see Subagents.
- Start → In Progress. PR open → In Review, plus a comment with the PR link and manual test path.
- Linear's GitHub integration can flip a ticket back to In Progress when its PR links. Set In Review after the PR shows on the ticket, and check again before the final message.
- In Review means someone is reviewing. Merging or closing the PR moves the ticket to Done automatically. Review comments move it back to In Progress: address them, push, and set In Review again.
- Never set Done yourself.
- New findings, decisions, scope changes, or bugs you spot go into Linear: a comment on the ticket, or a new ticket. Not only chat.
- If a ticket looks wrong, conflicts with existing code, or is a bad idea, say so before building it.

## Git
- Branch off latest master: feat/, fix/, chore/, refactor/, docs/ + short slug. One branch and PR per ticket unless tickets are tightly coupled.
- Push only to your branch. Never commit to, push to, or merge into master. Don't merge PRs.
- Put the ticket id in the PR title so Linear links it.

## Code
- Code should read on its own. Comments are rare, short, and only for a non-obvious "why".
- No ticket ids in code, comments, or docs.
- Docs stay short and plain.
- Run the repo's tests, lint, and type checks before pushing.

## Cross-repo changes
- If the frontend depends on a backend change, say so in both PRs. The backend merges and deploys (migrations run on the homelab) before the frontend merges.

## PR description
- Summary: a few lines.
- Deploy notes: migrations, env vars, merge/deploy order.
- Manual test path: numbered steps from a clean state, expected result per step, edge cases, and what broken looks like.

## Final message after a batch
- Every PR link.
- Per PR, the manual testing walkthrough.
- Merge/deploy order.
- Open questions and what you changed in Linear.

## Models
- Sonnet by default: implementation, UI work, tests, refactors that follow an existing pattern, Linear updates.
- Opus for the design step when the ticket involves:
  - a new abstraction: base class, interface, plugin/adapter layer, shared state store
  - data model or migration changes
  - WebSocket/API contract changes between server and frontend
  - auth, permissions, crypto, E2EE, anything security-sensitive
  - concurrency or realtime ordering: presence, read state, voice/LiveKit sync
  - a bug Sonnet hasn't fixed after two attempts
- Opus writes the design: types/interfaces, file layout, edge cases. Sonnet implements it. Don't run the whole ticket on Opus.
- Say in the PR which parts were designed on Opus.

## Subagents
- The main session plans, owns git, PRs and Linear, and writes the final message. Subagents do the rest.
- Read-only work goes to subagents, several at once when the questions are independent: finding code, tracing a flow, checking a ticket against master, reading logs.
- One subagent writes at a time. Never two writers on this checkout, and none here while one writes in the sibling repo.
- Per ticket: explore, design (Opus, only where Models says so), implement (Sonnet), review by a fresh subagent that gets the ticket and the diff but not the implementer's reasoning, fix.
- A brief stands on its own: the goal, the files, the rules from this file, what to hand back. The subagent has not seen the conversation.
- Subagents don't push, open PRs or touch Linear.
- Their reports are claims. Check what matters (run the test, read the line) before it goes into a PR or a ticket.
- Reviewers report defects with a concrete trigger. No style, naming or "consider" notes.
- Skip subagents for a change that is a few lines.
