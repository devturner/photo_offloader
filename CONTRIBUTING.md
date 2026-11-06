# Contributing

Bug reports and pull requests are welcome. Photo Offloader looks after people's photos, so protecting
them always comes before tidier code.

## Reporting a bug

[Open an issue](https://github.com/devturner/photo_offloader/issues) with:

- what you did, what you expected and what happened
- your macOS and Python versions, and the `VERSION` file from your clone
- the relevant lines from `~/.camera_transfer.log` (or **⋯ › View logs** in the dashboard)

Logs contain file paths and card names, so check them before you post them.

## Proposing a change

For anything large, open an issue first so we can agree the approach. That includes support for other
systems: Photo Offloader only runs on macOS for now.

1. Set up a clone and run the tests, as described in [Development](docs/development.md). If the app is
   installed on your Mac, read the rules on that page first, because a source checkout uses your real data.
2. Create a branch from `main`.
3. Write a failing test and check it fails for the right reason, then make the change. For important
   fixes, break the code again and confirm the test goes red.
4. Base claims on measurements, such as logs or timings on real data. Benchmark against a copy of the
   catalog.
5. Record behaviour changes in `CHANGELOG.md` under `[Unreleased]`, and in `docs/`.
6. Keep commits reviewable and explain why in the message. Keep behaviour changes separate from moves and
   renames.
7. Open a pull request. Pull requests are merged with a merge commit, so your commits stay as they are.

The [code quality guide](CODE_QUALITY.md) has the full checklist, and the
[safety model](docs/how-it-works.md#the-safety-model) lists what a change must never break.
