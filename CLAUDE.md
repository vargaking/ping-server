# Working on this repo

## Tickets (Linear: team Zeta Chat, project Web Alpha)
- Linear is the source of truth. A dated "Update <date>" section in a ticket overrides older text.
- Work one ticket at a time. Never run parallel agents on this checkout.
- Start → In Progress. PR open → In Review, plus a comment with the PR link and manual test path. Never set Done; Dániel does that after testing.
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
