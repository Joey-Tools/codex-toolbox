#!/usr/bin/env bash

set -euo pipefail

readonly RETAINED_TEMP_EXIT=75

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

if [[ ${1-} != "--" || $# -lt 2 ]]; then
  fail "usage: $0 -- <command> [args ...]"
fi
shift

current_uid="$(id -u)"
readonly current_uid

if [[ ${HOME-} != /* ]]; then
  fail "HOME must be an absolute path"
fi
trusted_home="$(cd -P -- "$HOME" && pwd -P)"
readonly trusted_home
if [[ "$trusted_home" != "$HOME" ]]; then
  fail "HOME must already name its canonical physical directory"
fi

if stat -c '%u' / >/dev/null 2>&1; then
  stat_dev_inode() {
    stat -c '%d:%i' "$1"
  }

  stat_owner() {
    stat -c '%u' "$1"
  }

  stat_mode() {
    stat -c '%a' "$1"
  }
else
  stat_dev_inode() {
    stat -f '%d:%i' "$1"
  }

  stat_owner() {
    stat -f '%u' "$1"
  }

  stat_mode() {
    stat -f '%Lp' "$1"
  }
fi

validate_trusted_home() {
  local path owner mode parent
  path="$trusted_home"
  while true; do
    if [[ -L "$path" || ! -d "$path" ]]; then
      printf 'error: trusted HOME ancestor is not a real directory: %s\n' \
        "$path" >&2
      return 1
    fi
    owner="$(stat_owner "$path")" || return 1
    mode="$(stat_mode "$path")" || return 1
    if [[ "$path" == "$trusted_home" && "$owner" != "$current_uid" ]]; then
      printf 'error: trusted HOME is not owned by the current uid: %s\n' \
        "$path" >&2
      return 1
    fi
    if [[ "$path" != "$trusted_home" \
      && "$owner" != "0" && "$owner" != "$current_uid" ]]; then
      printf 'error: trusted HOME ancestor has an unsafe owner: %s\n' \
        "$path" >&2
      return 1
    fi
    if (( (8#$mode & 0022) != 0 )); then
      printf 'error: trusted HOME ancestor is group/world writable: %s\n' \
        "$path" >&2
      return 1
    fi
    if [[ "$path" == "/" ]]; then
      return 0
    fi
    parent="$(dirname "$path")"
    if [[ "$parent" == "$path" ]]; then
      printf 'error: trusted HOME ancestor traversal did not reach root: %s\n' \
        "$path" >&2
      return 1
    fi
    path="$parent"
  done
}

validate_trusted_home || exit 2

private_tmp="$(mktemp -d "$trusted_home/.codex-personal-sync-ci.XXXXXX")"
tmp_object_identity=""
tmp_owner=""
tmp_mode=""

cleanup() {
  local command_status current_identity current_owner current_mode cleanup_status
  command_status=$?
  cleanup_status=0
  trap - EXIT

  if ! validate_trusted_home; then
    printf 'error: retained CI temp root after trusted HOME drift: %s\n' \
      "$private_tmp" >&2
    cleanup_status=$RETAINED_TEMP_EXIT
  elif [[ -L "$private_tmp" || ! -d "$private_tmp" ]]; then
    printf 'error: retained CI temp root after type/replacement drift: %s\n' \
      "$private_tmp" >&2
    cleanup_status=$RETAINED_TEMP_EXIT
  elif ! current_identity="$(stat_dev_inode "$private_tmp"):directory"; then
    printf 'error: retained CI temp root after identity became unreadable: %s\n' \
      "$private_tmp" >&2
    cleanup_status=$RETAINED_TEMP_EXIT
  elif [[ -z "$tmp_object_identity" \
    || "$current_identity" != "$tmp_object_identity" ]]; then
    printf 'error: retained replacement at CI temp root: %s\n' \
      "$private_tmp" >&2
    cleanup_status=$RETAINED_TEMP_EXIT
  elif ! current_owner="$(stat_owner "$private_tmp")" \
    || ! current_mode="$(stat_mode "$private_tmp")"; then
    printf 'error: retained CI temp root after access policy became unreadable: %s\n' \
      "$private_tmp" >&2
    cleanup_status=$RETAINED_TEMP_EXIT
  elif [[ "$current_owner" != "$tmp_owner" || "$current_mode" != "$tmp_mode" ]]; then
    printf 'error: retained CI temp root after access policy drift: %s\n' \
      "$private_tmp" >&2
    cleanup_status=$RETAINED_TEMP_EXIT
  # POSIX has no conditional rmdir. This check-to-rmdir boundary assumes a
  # cooperative single-uid CI runner; non-recursive removal cannot delete
  # replacement contents, and observed identity/policy drift is retained.
  elif ! rmdir "$private_tmp"; then
    printf 'error: retained non-empty CI temp root: %s\n' \
      "$private_tmp" >&2
    cleanup_status=$RETAINED_TEMP_EXIT
  fi

  if (( command_status != 0 )); then
    exit "$command_status"
  fi
  exit "$cleanup_status"
}
trap cleanup EXIT

if [[ -L "$private_tmp" || ! -d "$private_tmp" ]]; then
  fail "CI temp root is not a real directory: $private_tmp"
fi
initial_dev_inode="$(stat_dev_inode "$private_tmp")"
tmp_owner="$(stat_owner "$private_tmp")"
tmp_mode="$(stat_mode "$private_tmp")"
if [[ "$(stat_dev_inode "$private_tmp")" != "$initial_dev_inode" ]]; then
  fail "CI temp root changed during initial binding: $private_tmp"
fi
tmp_object_identity="$initial_dev_inode:directory"
if [[ "$tmp_owner" != "$current_uid" ]]; then
  fail "CI temp root is not owned by the current uid: $private_tmp"
fi
if [[ "$tmp_mode" != "700" ]]; then
  fail "CI temp root is not mode 0700: $private_tmp"
fi

validate_trusted_home || exit 2
if [[ -L "$private_tmp" || ! -d "$private_tmp" \
  || "$(stat_dev_inode "$private_tmp"):directory" != "$tmp_object_identity" ]]; then
  fail "CI temp root changed before test launch: $private_tmp"
fi
if [[ "$(stat_owner "$private_tmp")" != "$tmp_owner" \
  || "$(stat_mode "$private_tmp")" != "$tmp_mode" ]]; then
  fail "CI temp root access policy changed before test launch: $private_tmp"
fi

TMPDIR="$private_tmp" "$@"
