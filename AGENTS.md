# AGENTS.md

## Core rules
- Keep the implementation minimal.
- Do not add features outside the requested scope.
- Do not introduce a database.
- Keep the project file-based with one-race-per-JSON.
- Separate prediction from simulation.
- Use LLM only for prediction and feedback summarization.
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
- feedback

Do not add extra top-level sections unless explicitly requested.

## Working style
- Implement the smallest useful slice first.
- Stop when the requested scope is complete.
- Do not pre-build future features.
- Keep README and config aligned with the implementation.
