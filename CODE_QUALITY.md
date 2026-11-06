# Code quality guide

What good code looks like in this project. This guide covers judgement calls. Commands, the
architecture and the safety invariants are in [CLAUDE.md](CLAUDE.md), and they win if the two
ever disagree. Protecting someone's photos comes before tidiness, so a rule here never
justifies weakening a safety check.

## Before you open a PR

- [ ] A test failed first, for the right reason, and important fixes were mutation-checked.
- [ ] Each commit is one reviewable unit, and moves or renames are separate from behavior changes.
- [ ] Nothing was copied that could have been shared, or the copy is explained.
- [ ] A failure in the new code leaves a log line that explains it without a debugger.
- [ ] Anything a person sees says what happened and what to do next.
- [ ] The code you touched is at least as clear as you found it.
- [ ] `CHANGELOG.md` and `README.md` cover any behavior change.

## Clean code

- **Name things for what they mean to the reader.** `copy_local_archive_to_usb` needs no comment.
- **One function, one job.** ruff fails anything over complexity 15; split it, don't raise the
  limit. Length isn't enforced, but past about 60 lines, look for the seam.
- **Keep the import direction.** Modules only import downward and call each other qualified
  (`catalog.catalog_row(...)`), so a reader can always see where a name comes from.
- **Comment on why, not what.** Record the constraint or the failure that shaped the code.
  Do the same for anything that looks wrong but is deliberate, so nobody "fixes" it.
- **Delete, don't disable.** No commented-out code or unused options; git keeps the history.
  Open work goes in `scratch/ai_tasks.md`, and the code currently has no `TODO` comments.
- **Build only what's needed now.** Don't handle cases that can't happen or add settings nobody
  has asked for. Every addition has to be reviewed and maintained.

## DRY and reuse

- **Each fact has one home.** Paths and settings live in `config.py`, JSON state files in
  `jsonfile.py`, logging helpers in `logs.py`, dashboard wording in `dashboard/display.py`, and
  its state and actions in `dashboard/core.py`. Look there before adding a helper.
- **Share rules, not resemblance.** Merge code when it has to change together. Two blocks that
  only look alike today can stay separate, and on a third copy, stop and extract it.
- **Share with a small, plain interface.** Prefer functions that take their inputs and return
  data, like `menubar/model.py`, which can be tested without launching anything.
- **Some duplication is deliberate.** Shell scripts can't read `config.py`, so they repeat its
  paths; change both sides together. Each script's `log` helper also stays its own, because
  they behave differently: `trigger.sh` buffers, `import_supervisor.sh` writes two files and
  `uninstall.sh` has levels. The overlapping trigger and session locks stay too.

## Leave it better than you found it

- Fix small things in the code you're already changing: a misleading name, a stale comment, a
  missing test around a bug you just found.
- Put the cleanup in its own commit so the behavior change stays easy to review.
- A larger problem gets a note in `scratch/ai_tasks.md`, not a surprise rewrite inside a bug fix.

## Maintainability

- **Tests describe behavior.** Name them for what must stay true, and patch a name in the
  module that owns it. Tests never touch the real install (see CLAUDE.md).
- **Measure before you claim.** Performance and root-cause claims need a log, a timing or a
  reproduction, ideally quoted in the commit message.
- **Commit messages explain why.** The diff already shows what changed.
- **Stay portable.** Target Python 3.9 and the bash 3.2 that ships with macOS.
- **Think twice about dependencies.** A new required runtime dependency needs asking first.

## Errors

- **Catch what you can name.** Prefer `OSError` or `sqlite3.Error` to `Exception`.
- **Catch `Exception` only at a boundary,** where one failure must not take down the rest. For
  example, a failed USB stage is recorded and logged, and the verified local import stands.
- **Never fail silently.** Each `except` logs, records the failure, returns a default its function
  promises (`read_object` gives `{}`), or has a comment saying why doing nothing is right.
- **Don't call it success unless it was verified.** Report partial results as partial. A status
  of "done" has to mean the data is safely there.

## Helpful logging

Most of this runs unattended under launchd, so the logs are often the only witness.

- **Python uses `logs.logger`,** with lazy arguments: `logger.warning("Could not rotate %s: %s",
  path, exc)`, not an f-string.
- **Pick the level by who has to act:**

  | Level | Use it for |
  | --- | --- |
  | `debug` | Detail that only helps when diagnosing a problem |
  | `info` | Milestones and summaries: scan totals, stage start and finish |
  | `warning` | Something failed, and the run can continue |
  | `exception` | An unexpected failure, inside an `except`, so the traceback is kept |

- **Include the facts someone will need:** the path or volume, the counts and the underlying
  error. "Copy failed" isn't enough; say which file, where to and why.
- **Log per stage, not per file,** at `info`. A card can hold thousands of files, and the
  summary should be easy to find.
- **Keep secrets out,** such as the dashboard token or rclone credentials.
- **Shell scripts log timestamped lines** through their own `log` function and keep the original
  error from the command that failed.

## Ease of use

- **Write for the person holding the camera card.** Say what happened, whether their photos are
  safe and what to do next. A good example from the importer: "The card was copied to the Mac.
  USB backup needs attention."
- **Use plain language in the UI.** Keep tracebacks and internal names in the logs.
- **Make safe the default.** Risky actions should be hard to trigger by accident, and normal use
  shouldn't need a setting.
- **Keep the page calm.** Hide controls until they're needed, keep the transfer panel one fixed
  size, and let nothing flash in or out.
- **Treat card-derived text as untrusted.** The dashboard uses `textContent`, never `innerHTML`.

## What's enforced

CI runs these, so don't spend review time on them:

| Check | Catches |
| --- | --- |
| `ruff check .` | Lint errors, import order, complexity over 15 |
| `ruff format --check .` | Code that isn't formatted; run `ruff format .` to fix it |
| `unittest discover` | Regressions, on Python 3.9, 3.11, 3.13 and 3.14 |
| `bash -n` | Shell syntax errors |
| `compileall` / `py_compile` | Python that doesn't compile, including `bin/` |

Everything else in this guide is for the author and the reviewer. If a rule keeps getting
missed and a tool could catch it, add the tool.

## Known gaps

Where the code doesn't meet this guide yet, measured on 2026-11-05. Improve these when you're
working nearby. They aren't a reason to copy the pattern.

- `dashboard/core.py` is still the largest module, at 666 lines.
