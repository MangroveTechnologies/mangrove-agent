# Git Workflow

## Branches & PRs

- NEVER commit to `main`/`master`. Create a `feature/`, `fix/`, or `audit/` branch first.
- Every merge to main is via PR. Human approval required.
- AI agents must NEVER run `gh pr merge` -- approve only; the human merges.
- First push uses `-u` for upstream tracking.

## Post-merge

- Watch CI: `gh run list --limit 5` or `gh run watch`. Don't declare done until CI passes.
- Verify deployment (health check or smoke test).
- Delete branch remote (`git push origin --delete <branch>`) and local (`git branch -d <branch>`).
- `git checkout main && git pull origin main`.
