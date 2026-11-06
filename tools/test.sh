#!/bin/bash
# Run the tests at one of two levels. See "Commands" in CLAUDE.md for when to use each.
#   fast  everything except the modules that take most of the time (about 35 s)
#   full  everything, including the browser tests, as CI does (about 2.5 min)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SLOW_MODULES=" test_trigger test_service_control " # tests/browser isn't listed at the top level

usage() {
  echo "Usage: tools/test.sh fast [--list] | full" >&2
  exit 2
}

cd "$ROOT"
case "${1:-}" in
fast)
  modules=()
  for path in tests/test_*.py; do
    name="$(basename "$path" .py)"
    case "$SLOW_MODULES" in
    *" $name "*) ;;
    *) modules+=("tests.$name") ;;
    esac
  done
  if [ "${2:-}" = "--list" ]; then
    printf '%s\n' "${modules[@]}"
    exit 0
  fi
  exec .venv/bin/python -m unittest "${modules[@]}"
  ;;
full)
  exec .venv/bin/python -m unittest discover
  ;;
*)
  usage
  ;;
esac
