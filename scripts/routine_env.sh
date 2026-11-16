#!/usr/bin/env bash
# Reads the optional local settings file routine.env in the root of the checkout. Sourced by
# capture_routine.sh and backup_state.sh; never run on its own.
#
# Format: KEY=VALUE lines. Only MBQ_DATA_ROOT and QUANT_PYTHON are read. Every other key, blank lines and
# lines that start with # are ignored. A variable that is already set in the environment wins over the file.
# One trailing carriage return is stripped. The value is taken literally: no quotes, no expansion, no
# command substitution. The file is parsed line by line and is never sourced or evaluated.

# load_routine_env ROOT_DIR : set MBQ_DATA_ROOT and QUANT_PYTHON from ROOT_DIR/routine.env when they are unset.
load_routine_env() {
  local file="$1/routine.env" line key value
  [[ -f "$file" && -r "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    case "$line" in ""|"#"*) continue ;; esac
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      MBQ_DATA_ROOT) [[ -n "${MBQ_DATA_ROOT:-}" ]] || MBQ_DATA_ROOT="$value" ;;
      QUANT_PYTHON)  [[ -n "${QUANT_PYTHON:-}" ]]  || QUANT_PYTHON="$value" ;;
    esac
  done < "$file"
  return 0
}
