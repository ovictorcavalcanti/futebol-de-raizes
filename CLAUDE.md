# Agent operating guide

## Purpose

This repository should be handled with a minimal context, minimal command, and targeted validation strategy.
The goal is to make correct changes without repeatedly scanning the repository, loading unrelated files, or running expensive validation pipelines.
Prefer the smallest amount of context and execution needed to safely complete the task.

## Core principles

1. Start narrow.
2. Search before reading.
3. Read only what is necessary.
4. Follow direct dependencies only.
5. Make localized changes whenever possible.
6. Validate proportionally to the change.
7. Prefer targeted tests over full suites.
8. Do not repeat successful validation unnecessarily.
9. Avoid long running background processes.
10. Do not expand task scope unless required for correctness.

## Repository exploration

Do not begin a task by broadly exploring the repository.
Do not recursively inspect the entire project structure unless the user explicitly asks for architectural analysis.
If the user provides file paths, start with those files only.
If the relevant files are unknown, locate them using targeted search.

Prefer `rg "MatchHeader" frontend/` over `rg "MatchHeader" .`

Prefer searching by: symbol name, model name, route name, endpoint, CSS class, component name, function name, test name, URL path.

After finding likely files, inspect only the most relevant ones.
Do not open many search results speculatively.

## Context limits

Keep the working context small.
For a normal localized change, aim to work primarily with fewer than 10 source files.
Opening additional files is acceptable when they are directly required by: imports, inheritance, types, interfaces, foreign keys, API contracts, templates, selectors, services, routes, tests directly covering the behavior.

Do not read unrelated files for general context.
Do not reread unchanged files unless there is a concrete reason.
Do not load entire large files when only one relevant section or symbol is needed.
Do not inspect whole directories merely to become familiar with them.

## Search strategy

Use targeted search before opening files.

Recommended workflow:

```
identify feature
→ search relevant symbol
→ locate implementation
→ inspect direct dependencies
→ modify
→ validate narrowly
```

Avoid:

```
scan repository
→ inspect architecture
→ read many related modules
→ inspect all tests
→ begin implementation
```

Searches should normally be scoped to the most likely application or directory, e.g. `rg "Competition" competitions/`, `rg "standings" standings/`, `rg "MatchAdmin" matches/`.
Avoid unrestricted repository wide searches unless a scoped search returns insufficient information.

## Command budget

For a normal localized task, aim to use fewer than 30 shell commands.
This is not a strict technical limit, but exceeding it should trigger reassessment.
Before continuing past roughly 30 commands, consider whether:

1. The task scope has expanded unnecessarily.
2. The same information is being searched repeatedly.
3. Tests are being rerun unnecessarily.
4. A more targeted command could replace several broad commands.
5. Temporary debugging has become excessive.

Large refactors and architectural changes may legitimately require more commands.

## Change scope

Prefer the smallest change that satisfies the request.
Do not refactor unrelated code while implementing a localized feature or bug fix.
Do not rename, reorganize, or clean up unrelated modules unless required for correctness.
Do not modify neighboring systems merely because improvements are possible.
If a task reveals unrelated technical debt, leave it unchanged unless it blocks the requested work.

## Django specific guidance

For Django changes, inspect only the relevant application first.
Typical dependency order:

```
model
→ selector/query
→ service/domain logic
→ view/API
→ template/frontend
→ directly related tests
```

Do not automatically inspect every layer. Only follow layers involved in the requested behavior.
Avoid reading unrelated migrations, fixtures, management commands, admin modules, signals, middleware or settings unless the task directly depends on them.

## Models and migrations

When changing Django models, determine whether a migration is actually required.
Do not inspect the entire migration history.
Prefer `python manage.py makemigrations app_name` when appropriate.
Do not run migrations across unrelated apps simply to validate a localized code change unless required.
Do not create migrations for formatting, method, property, admin, selector, service, or frontend only changes.

## Database operations

Avoid expensive database recreation unless required.
Prefer reusing an existing test database when supported:

- pytest-django: `pytest --reuse-db path/to/test.py`
- Django test runner: `python manage.py test app.tests --keepdb`

Do not repeatedly destroy and recreate test databases during the same task.

## Seed commands

Do not run seed or reset commands unless required by the task.
Seed commands may delete and recreate substantial amounts of data.
If seed behavior is relevant, inspect the specific management command first.
Avoid repeatedly running `python manage.py seed --reset` during normal implementation.

## Test policy

Testing must be proportional to the change.
Do not run the full test suite as a baseline before implementation.
Do not run the full test suite during normal iteration.
Run the smallest test scope that can reasonably validate the changed behavior.

Preferred order:

```
single test
→ test class
→ test file
→ application tests
→ related integration tests
→ full suite only when justified
```

Examples: `pytest tests/matches/test_services.py::test_update_score`, `pytest tests/matches/test_services.py`, `python manage.py test matches.tests.test_services.MatchServiceTests`.
Avoid by default: `pytest`, `python manage.py test`.

## Full test suite

The full test suite is not part of the default development loop.
Run the complete suite only when one of the following applies:

1. The user explicitly requests it.
2. The change modifies shared core infrastructure.
3. The change affects behavior across multiple applications.
4. The change modifies global settings or shared framework behavior.
5. Targeted tests reveal evidence of wider regression risk.
6. The task is a large refactor whose impact cannot be reasonably isolated.

Do not run the full suite more than once per task unless new changes after the previous run materially affect broad behavior.
A passing full suite remains valid for unchanged areas.
Do not rerun the complete suite because of copy changes, CSS changes, minor template adjustments, comments, documentation, formatting, small admin presentation changes or other cosmetic changes.

## Baseline testing

Do not automatically run the full suite before making changes.
If baseline validation is useful, run the relevant existing tests only (e.g. `pytest tests/matches/test_admin.py` instead of `pytest`).
A full baseline suite should only be used when diagnosing repository wide failures or when explicitly requested.

## Test creation

Add or update tests only for behavior affected by the task.
Do not create broad test coverage unrelated to the requested change.
Prefer extending an existing relevant test file over creating many new files.
Do not rewrite large test areas unless the implementation requires it.
Keep new tests focused on behavior, not internal implementation details unless necessary.

## Failed tests

When a targeted test fails:

1. Read the failure carefully.
2. Investigate only the related code path.
3. Make the smallest correction.
4. Rerun the failed test first.
5. Run the related test file after the specific failure passes.

Do not immediately run the full suite after fixing one targeted failure.

## Slow tests

If a test suite is slow, identify expensive tests before repeatedly running it (`pytest --durations=20`).
Use targeted tests during implementation.
Full suite runtime should not become part of every edit cycle.

## E2E policy

Do not run E2E tests by default.
Run E2E tests only when:

1. The user explicitly requests them.
2. The change directly affects a browser workflow.
3. Unit or integration tests cannot reasonably validate the behavior.
4. The task changes JavaScript interaction, browser navigation, forms, or end user UI behavior that requires browser verification.

When E2E testing is justified, run only the relevant test: prefer `pytest tests/e2e/test_admin.py::test_competition_navigation` over `pytest tests/e2e`.
Do not run the complete E2E suite for a localized change.

## Browser automation

Do not launch browsers unless browser verification is necessary.
Do not take screenshots for backend only changes.
Do not retake screenshots after unrelated or cosmetic changes unless visual verification is part of the task.
Do not use browser automation merely as an additional confidence check when unit or integration tests already validate the behavior.

## Screenshot policy

Screenshots should only be generated when the user requests visual validation, the task changes layout, the task changes styling, the task changes browser rendered behavior, or a visual regression needs investigation.
Do not repeatedly regenerate screenshots after each small change.

## Frontend testing

Use targeted frontend tests (e.g. `node --test tests/js/standings.test.mjs` rather than every JS test).
Do not run the entire frontend test suite for a localized component change unless necessary.

## Frontend linting

Lint only modified or directly affected files whenever possible, not the whole project.

## Python linting

Prefer targeted linting, e.g. `ruff check matches/services.py matches/selectors.py`.
Avoid by default: `ruff check .`

## Formatting

Format only changed files when possible.
Do not reformat unrelated directories.
Avoid formatting changes that create large unrelated diffs.

## Type checking

Do not automatically run repository wide type checking for localized changes.
If the tool supports file or package scoped type checking, use the narrowest useful scope.
Run project wide type checking only when shared type definitions changed, public interfaces changed, the user requests it, or the change has broad typing impact.

## Build commands

Do not run full production builds (e.g. `collectstatic` in production mode) automatically for every frontend change.
Run a complete build when:

1. The user explicitly requests it.
2. Build configuration changed.
3. Bundling behavior changed.
4. A production build is necessary to verify the task.

Otherwise prefer targeted tests and linting.

## Background processes

Avoid long running validation commands in the background.
Do not start a full suite in the background while continuing implementation.
This makes it easy to waste resources validating code that will change before the test run finishes.
Prefer synchronous, targeted validation.
Do not create polling loops to wait for background test processes (e.g. `until grep -q "passed" output.log; do sleep 5; done`). Run the relevant command directly instead.

## Process management

Do not leave development servers, browsers, workers, or test processes running unnecessarily.
If a temporary server is started for validation, stop it when no longer needed.
Avoid starting duplicate development servers.
Check whether an existing process can be reused before launching another one.

## Debugging

Use the smallest debugging intervention possible.
Prefer targeted logging, a single shell inspection, a single database query, a single failing test, or direct function invocation over broad instrumentation.
Do not add temporary debug prints to many files.
If temporary debug code is added, remove it before finishing.
Do not permanently modify tests solely to inspect runtime output.

## Temporary files

Avoid creating helper scripts and temporary files unless they materially simplify the task.
If a temporary script is created, delete it when the investigation is complete.
Do not leave files such as `debug.py`, `tail.py`, `tmp_test.py`, `scratch.py`, `output.txt` unless they are intentionally part of the project.

## Admin changes

For Django admin changes, begin with the relevant `admin.py`, the relevant model and the relevant admin tests.
Do not inspect unrelated applications unless the admin behavior depends on them.
Browser based admin validation should only be used if the requested behavior cannot be adequately verified through tests or code inspection.

## API changes

For API changes, inspect only: endpoint, serializer/schema, service/domain logic, selector/query logic, related model if necessary, related tests.
Do not automatically inspect the entire API package.
Validate with the smallest relevant test set.

## Service and domain changes

When changing business logic, prioritize the domain or service tests that directly exercise it.
Avoid testing through a browser when the behavior can be validated at the service or API layer.
Prefer lower level tests when they provide sufficient confidence.

## JavaScript changes

For localized JavaScript changes:

1. Inspect the relevant file.
2. Inspect its direct imports if required.
3. Run the directly related frontend test if one exists.
4. Run targeted linting.
5. Avoid full E2E unless browser behavior genuinely requires it.

## CSS and presentation changes

For CSS, template, or presentation only changes:
Do not run the full backend test suite.
Do not run unrelated database tests.
Use targeted frontend validation.
Use browser or screenshot validation only when appropriate.

## Documentation changes

For documentation only changes:
Do not run application tests unless documentation generation itself is executable and affected.
Do not run the full suite.

## Refactors

For refactors, establish the intended behavioral boundary before changing code.
Use existing targeted tests as regression protection.
Expand validation gradually according to the scope of the refactor.
Large refactors may justify broader testing, but repeated full suite runs during implementation should still be avoided.

## Architecture changes

For genuine architecture or system design tasks, broader repository exploration may be justified. Even then:

1. Search before reading.
2. Identify relevant domains first.
3. Avoid reading unrelated implementation details.
4. Build a dependency map incrementally.
5. Do not assume every application needs inspection.

Architecture analysis is an exception to the normal narrow exploration policy, not the default.

## Repository map

If a repository map or architecture document exists, use it before exploring source directories broadly.
In this repository: `README.md` (architecture and module map), `docs/CONTRACT.md` (interfaces and JSON shapes), `docs/FRONTEND.md` (front-end hooks), `docs/IDENTIDADE.md` (visual identity), `docs/PLANO.md` (original plan).
These files should be preferred for high level orientation.
Do not use them as a reason to load the entire repository afterward.

## Git usage

Inspect the current diff before making broad changes: `git status --short`, `git diff --stat`, `git diff -- path/to/file`.
Avoid repeatedly dumping very large diffs into context. Use file scoped diffs where possible.
Do not modify unrelated existing user changes.

## Existing changes

Assume uncommitted changes may belong to the user.
Do not revert, overwrite, reset, or clean unrelated modifications.
Do not use destructive Git commands (`git reset --hard`, `git clean -fd`) unless explicitly requested.

## Generated files

Do not inspect generated files unless relevant: `node_modules`, `dist`, `build`, `coverage`, `staticfiles`, `__pycache__`, `.pytest_cache`, compiled assets, generated bundles, large lock file internals.
Lock files may be updated when dependencies change, but should not be read in full unless necessary.

## Migrations

Do not browse migration histories by default.
Inspect a specific migration only if a migration is failing, schema history matters, a data migration is involved, or a dependency conflict exists.
Do not load dozens of historical migration files for context.

## Fixtures

Do not inspect large fixture datasets unless the task depends on their contents.
Prefer targeted test factories (`tests/factories.py`) or specific fixture entries.

## Logs

When investigating logs, search for the relevant error or identifier first (`rg "IntegrityError" application.log`, `tail -n 100 application.log`).
Do not load an entire large log file unless required.

## Database inspection

Use targeted queries (e.g. `SELECT id, status FROM matches WHERE id = ...;`).
Do not dump whole tables unless the task requires it.

## Validation hierarchy

```
static reasoning
→ targeted unit test
→ targeted integration test
→ targeted lint
→ targeted browser test
→ broader application tests
→ full suite
```

Do not jump directly from implementation to full suite.

## Validation after cosmetic changes

If broad tests already passed and subsequent changes are purely cosmetic (copy, labels, spacing, CSS, display formatting, comments, nonfunctional template adjustments), do not rerun the entire test suite.
Run only the relevant presentation validation if necessary.

## Repeated validation

Do not rerun a passing test if none of the code affecting that test has changed.
Do not rerun identical validation simply for extra confidence.
Every repeated expensive command should have a concrete reason.

## Final validation

Before finishing a localized task, usually perform:

1. Relevant targeted tests.
2. Targeted linting if applicable.
3. Review of the final diff.

Do not automatically add a full backend suite, full frontend suite, full E2E suite, production build, screenshots or browser walkthrough unless justified by the task.

## Full suite maximum

Unless explicitly requested or required due to broad impact: **maximum full test suite executions per task: 1.**
A full suite should normally happen only near the end, not at the beginning.

## E2E maximum

Unless explicitly requested: **do not run the entire E2E suite.** Use a single relevant E2E test when required.

## Long running commands

Avoid commands expected to take several minutes unless they are necessary.
If a narrower alternative exists, use it (e.g. `pytest tests/test_standings_services.py` instead of `pytest`).

## Failure recovery

When something fails, do not restart the entire workflow. Continue from the smallest failed unit:

```
targeted test fails
→ inspect failure
→ fix
→ rerun that test
→ run its test file
→ finish
```

Avoid: targeted test fails → modify code → full suite → E2E → browser.

## User requested changes

If the user asks for a small change, treat it as a small change unless evidence proves otherwise.
Do not reinterpret a localized request as a repository wide cleanup or redesign.
If multiple implementations are possible, prefer the one with the smallest blast radius.

## Completion criteria

A task is complete when:

1. The requested behavior is implemented.
2. The directly relevant tests pass.
3. No known regression exists in the affected area.
4. Temporary debugging artifacts are removed.
5. The diff contains no unrelated changes.

A task does not require every possible validation mechanism to be executed.

## Final response

When reporting completion, summarize: what changed, which files were materially changed, which targeted tests were run, and whether any broader validation was intentionally skipped.
Do not claim the entire application is regression free unless the relevant broad validation was actually performed.

## Default operating mode

Unless the task explicitly requires otherwise:

1. Identify the relevant feature.
2. Search for its implementation.
3. Open only the most relevant files.
4. Follow direct dependencies if necessary.
5. Implement the smallest correct change.
6. Run targeted tests.
7. Fix targeted failures if any.
8. Run targeted linting if applicable.
9. Review the diff.
10. Finish.

Do not perform a general repository exploration first.
Do not run a baseline full suite.
Do not repeatedly run the full suite.
Do not run E2E by default.
Do not launch browser automation by default.
Do not use broad validation merely because it is available.

## Default priority

When choosing between broader certainty and efficient targeted validation for a localized task, prefer targeted validation unless there is a concrete reason to believe the change has broader impact.
Correctness remains required, but validation should match the actual risk and scope of the change.
