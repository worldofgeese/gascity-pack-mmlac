#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'patrol: %s\n' "$1" >&2
  exit 1
}

parse_result() {
  local status
  jq -ers --arg mode "$1" --arg formula "$2" --arg assignee "$3" '
    def root_id:
      if type == "string" and test("\\A[A-Za-z0-9][A-Za-z0-9._-]*\\z")
      then . else error("invalid or missing root ID") end;
    if length == 1 then .[0] else error("expected exactly one JSON result") end
    | if type == "object" and .schema_version == "1" and .ok == true
      then . else error("expected a successful versioned result") end
    | if $mode == "list" then
        if (.roots | type) != "array" then error("missing roots array")
        elif (.roots | length) > 1 then error("multiple patrol roots; inspect before retrying")
        elif (.roots | length) == 0 then ""
        else .roots[0]
          | if .assignee == $assignee and .ref == $formula
               and .metadata["gc.formula_name"] == $formula
            then .id | root_id else error("lookup returned another formula or execution") end
        end
      else .root_id | root_id end
  ' || {
    status=$?
    printf 'patrol: invalid response; stop and inspect graph-class runs before retrying\n' >&2
    return "$status"
  }
}

main() {
  [[ $# -ge 2 ]] || fail "usage: patrol.sh ensure|new|burn <patrol-formula> [--var name=value | root-id]"
  local mode="$1" formula="$2" assignee output root script_dir
  local -a vars=()
  shift 2
  case "$mode" in ensure|new|burn) ;; *) fail "unknown operation: $mode" ;; esac
  case "$formula" in
    mol-deacon-patrol|mol-witness-patrol|mol-refinery-patrol) ;;
    *) fail "unsupported patrol formula: $formula" ;;
  esac
  assignee="${GC_ALIAS:-${GC_AGENT:-}}"
  [[ -n "$assignee" && "$assignee" != *[[:space:]]* ]] || fail "GC_ALIAS or GC_AGENT must name an execution"
  command -v gc >/dev/null || fail "gc is required"
  command -v jq >/dev/null || fail "jq is required"

  if [[ "$mode" == burn ]]; then
    [[ $# -eq 1 && "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "burn requires one exact root ID"
    exec gc wisp burn "$1" --formula="$formula" --assignee="$assignee" --force
  fi
  while [[ $# -gt 0 ]]; do
    [[ "$1" == --var && $# -ge 2 ]] || fail "only --var name=value is accepted when creating a patrol"
    [[ "$2" == *=* && -n "${2%%=*}" && "${2%%=*}" != patrol_script ]] \
      || fail "invalid or reserved formula variable"
    vars+=(--var "$2")
    shift 2
  done

  if [[ "$mode" == ensure ]]; then
    output=$(gc wisp list --formula="$formula" --assignee="$assignee" --json) || return $?
    root=$(printf '%s' "$output" | parse_result list "$formula" "$assignee") || return $?
    if [[ -n "$root" ]]; then
      printf '%s\n' "$root"
      return 0
    fi
  fi

  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return $?
  vars+=(--var "patrol_script=$script_dir/patrol.sh")
  # Capture first: a parser must never mask a failed cook's exit status.
  output=$(gc formula cook "$formula" "${vars[@]}" --json) || return $?
  root=$(printf '%s' "$output" | parse_result cook "$formula" "$assignee") || return $?
  gc bd update "$root" --assignee="$assignee" >&2 || return $?
  printf '%s\n' "$root"
}

main "$@"
