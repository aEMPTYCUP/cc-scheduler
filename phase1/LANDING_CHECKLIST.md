# Phase CI-0 Landing Checklist (Third-Party Models)

## Status
Ready to commit. Codex + Claude Code confirmed on third-party endpoints.

## Files to commit
- phase1/ (entire directory)
- .github/workflows/protocol-ci.yml

## Secrets to add in GitHub repo
- ANTHROPIC_API_KEY
- ANTHROPIC_BASE_URL
- OPENAI_API_KEY or CODEX_API_KEY
- OPENAI_BASE_URL

## First run
1. Push the above files.
2. Go to Actions → Multi-Agent Protocol CI → Run workflow.
3. Set simulate = true.
4. Confirm artifact protocol-audit is produced.
