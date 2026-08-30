# AGENTS.md

## Core rules
- Keep the implementation minimal.
- Do not add features outside the requested scope.
- Do not introduce a database.
- Keep the project file-based with one-race-per-JSON.
- Separate prediction from simulation.
- Use LLM only for prediction.
- Keep article generation template-based, not free-form LLM writing.

## Technical defaults
- Use Python.
- Keep output as static files.
- Do not add unnecessary frameworks, services, or abstractions.

## Data contract
Each race file must keep this top-level structure:
- meta
- race
- horses
- prediction
- simulation
- result
- evaluation

Do not add extra top-level sections unless explicitly requested.

## Working style
- Implement the smallest useful slice first.
- Stop when the requested scope is complete.
- Do not pre-build future features.
- Keep README and config aligned with the implementation.

## Testing
- For UI copy or CSS-only changes, do not add tests that pin full prose or exact CSS values.
- Assert exact display text only when the wording is an explicit fixed requirement.
- Prefer tests that directly cover the changed scope.
- Run the full suite for cross-cutting changes, substantial logic changes, or when explicitly requested.
- Do not run unrelated full suites for every minor display change.
- Do not run compileall or repository-wide whitespace/conflict-marker checks for minor copy or CSS changes unless the change warrants them.
