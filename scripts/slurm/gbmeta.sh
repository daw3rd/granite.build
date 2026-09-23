#!/usr/bin/env bash
#
# gbmeta - pretty-print granite.build tracking metadata from a SLURM job.
#
# granite.build packs build-tracking fields into a job's SLURM --comment as a
# single whitespace-free "key=value;key=value" token (see
# src/gbserver/environment/_skypilot_metadata.py). That token is compact by
# necessity: SkyPilot emits "#SBATCH --comment=<value>" unquoted, so the stored
# value can contain no spaces. This helper expands it into an aligned,
# human-readable table for display only.
#
# Usage:
#   gbmeta.sh <jobid>            # look the comment up via scontrol, fall back to sacct
#   scontrol show job <id> -o | gbmeta.sh   # or pipe any text containing the comment
#   sacct -j <id> -o Comment%-200 -Pn | gbmeta.sh
#
# Exit status:
#   0  metadata found and printed
#   1  no granite.build metadata found
#   2  usage error

set -euo pipefail

usage() {
  echo "usage: gbmeta.sh <jobid>   |   <command producing a Comment> | gbmeta.sh" >&2
  exit 2
}

# fetch_comment JOBID
# Echo the raw --comment value for a SLURM job, trying the live controller
# (scontrol) first and the accounting DB (sacct, for finished jobs) second.
fetch_comment() {
  local jobid="$1" comment=""
  if command -v scontrol >/dev/null 2>&1; then
    # scontrol -o prints one line of space-separated Key=Value pairs; our
    # comment value has no spaces, so it ends at the next whitespace.
    comment="$(scontrol show job "$jobid" -o 2>/dev/null \
      | grep -oE 'Comment=[^[:space:]]+' | head -n1 | cut -d= -f2- || true)"
  fi
  if [ -z "$comment" ] && command -v sacct >/dev/null 2>&1; then
    comment="$(sacct -j "$jobid" -o Comment%-200 -Pn 2>/dev/null \
      | grep -m1 '.' || true)"
  fi
  printf '%s' "$comment"
}

# render COMMENT
# Print each ";"-separated key=value pair on its own aligned line. Splitting
# each pair at the FIRST "=" keeps values that themselves contain "=" or "://"
# intact (e.g. step_uri=space://steps/foo).
render() {
  local comment="$1"
  # If a wider line was piped (e.g. a scontrol/sacct row), isolate just the
  # SLURM comment value: everything after "Comment=" up to the next whitespace.
  case "$comment" in
    *Comment=*)
      comment="${comment#*Comment=}"   # drop up to & including the first Comment=
      comment="${comment%%[[:space:]]*}"  # keep up to the next whitespace
      ;;
  esac
  # Emit one aligned line per key=value pair; count them so an empty/"(null)"
  # comment reports "not found" rather than printing nothing and succeeding.
  printf '%s' "$comment" | tr ';' '\n' | awk -F= '
    NF >= 2 && $1 ~ /^[a-z_]+$/ {
      key = $1; sub(/^[^=]*=/, "", $0); printf "  %-11s %s\n", key ":", $0; n++
    }
    END { exit (n ? 0 : 1) }
  '
}

main() {
  local comment
  if [ -t 0 ]; then
    # No stdin: require a job id argument.
    [ "$#" -eq 1 ] || usage
    comment="$(fetch_comment "$1")"
  else
    # Comment (or a line containing it) is piped in.
    comment="$(cat)"
  fi
  render "$comment" || { echo "no granite.build metadata found" >&2; exit 1; }
}

main "$@"
