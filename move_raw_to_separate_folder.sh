#!/usr/bin/env bash
# Move supported RAW photos from CameraImports to sibling CameraImportsRaw.
# Default behavior is a dry run. Pass --apply to perform moves.
set -euo pipefail

SOURCE_ROOT="${HOME}/Pictures/CameraImports"
DEST_ROOT="${HOME}/Pictures/CameraImportsRaw"
APPLY=0

usage() {
  cat <<'EOF'
Usage: move_raw_to_separate_folder.sh [--apply] [--help]

Preview RAW file moves from ~/Pictures/CameraImports to
~/Pictures/CameraImportsRaw, preserving each file's relative path.

Options:
  --apply   Perform the moves (default is preview only)
  --help    Show this help

Existing destination files are never overwritten. Conflicts are reported and
left untouched; source files are removed only when their move succeeds.
EOF
}

while (($#)); do
  case "$1" in
    --apply) APPLY=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ ! -d "$SOURCE_ROOT" ]]; then
  printf 'Source folder does not exist: %s\n' "$SOURCE_ROOT" >&2
  exit 1
fi

if (( APPLY )); then
  printf 'APPLY MODE: moving RAW files from:\n  %s\nto:\n  %s\n\n' "$SOURCE_ROOT" "$DEST_ROOT"
else
  printf 'DRY RUN: no files will be moved. Use --apply to perform these moves.\n'
  printf 'From: %s\nTo:   %s\n\n' "$SOURCE_ROOT" "$DEST_ROOT"
fi

seen=0
moved=0
conflicts=0
failed=0

while IFS= read -r -d '' source; do
  relative="${source#"$SOURCE_ROOT"/}"
  destination="$DEST_ROOT/$relative"
  ((seen+=1))

  if [[ -e "$destination" ]]; then
    printf 'CONFLICT (left untouched): %s\n  destination exists: %s\n' "$source" "$destination"
    ((conflicts+=1))
    continue
  fi

  if (( APPLY )); then
    if ! mkdir -p "$(dirname "$destination")"; then
      printf 'ERROR (could not create destination directory): %s\n' "$destination" >&2
      ((failed+=1))
      continue
    fi
    # -n is an extra no-clobber safeguard if a destination appears after the check.
    if mv -n "$source" "$destination" && [[ ! -e "$source" ]]; then
      printf 'MOVED: %s\n  -> %s\n' "$source" "$destination"
      ((moved+=1))
    else
      printf 'ERROR (move failed or destination appeared): %s\n' "$source" >&2
      ((failed+=1))
    fi
  else
    printf 'WOULD MOVE: %s\n  -> %s\n' "$source" "$destination"
    ((moved+=1))
  fi
done < <(find "$SOURCE_ROOT" -type f \\( \
  -iname '*.nef' -o -iname '*.nrw' -o -iname '*.arw' -o \
  -iname '*.cr2' -o -iname '*.cr3' -o -iname '*.dng' -o \
  -iname '*.raf' -o -iname '*.orf' -o -iname '*.rw2' -o \
  -iname '*.pef' -o -iname '*.srw' -o -iname '*.3fr' -o \
  -iname '*.fff' -o -iname '*.iiq' -o -iname '*.kdc' -o \
  -iname '*.mos' -o -iname '*.mef' -o -iname '*.rwl' -o \
  -iname '*.x3f' \\) -print0)

printf '\nSummary: %d RAW file(s) found; %d %s; %d conflict(s); %d error(s).\n' \
  "$seen" "$moved" "$([[ $APPLY -eq 1 ]] && printf 'moved' || printf 'would move')" "$conflicts" "$failed"

if (( ! APPLY )); then
  printf 'Nothing was changed. Review the preview, then rerun with --apply.\n'
fi

(( failed == 0 ))
