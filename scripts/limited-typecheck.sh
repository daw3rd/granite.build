#!/usr/bin/env bash
#
# Run pylint + mypy and report results only for files changed vs main.
#
# Two kinds of changed file are checked:
#   * src/gbserver/ files: `make staticcheck` runs over the whole package and its
#     output is grepped for each changed file.
#   * steps/ python files: each step (the directory holding step-template.yaml) is
#     an independent tree whose src/ modules are imported by flat name (the step's
#     test/conftest.py puts src/ on sys.path), and module names such as conftest.py
#     repeat across steps. So a single mypy/pylint run over steps/ cannot work;
#     instead the changed files of each step are checked from that step's root.

MY_LINE_BREAK="---------------------------"
REPO_ROOT="$(git rev-parse --show-toplevel)"

# Print the step root (nearest ancestor dir holding step-template.yaml) of a
# repo-relative path, or nothing if the path is not inside a step.
#   $1 - repo-relative file path
step_root_of() {
    local d
    d="$(dirname "$1")"
    while [[ "${d}" == steps/* ]]; do
        if [[ -f "${d}/step-template.yaml" ]]; then
            echo "${d}"
            return
        fi
        d="$(dirname "${d}")"
    done
}

# Run pylint on some files of one step, from the step root.
#   $1    - pylint --disable list for these files ("" for none)
#   $2... - files to check, relative to the step root
pylint_step_files() {
    local disable="$1"
    shift
    [[ $# -gt 0 ]] || return 0
    PYTHONPATH="src:${REPO_ROOT}/src:${REPO_ROOT}/test" \
        pylint --rcfile="${REPO_ROOT}/.pylintrc" ${disable:+--disable="${disable}"} "$@"
}

# Run pylint + mypy on some files of one step, from the step root, and print the
# results with paths rewritten to be repo-relative.
#   $1    - step root (repo-relative)
#   $2... - files to check, relative to the step root
# Imports of the step's own src/, gbserver (src/) and the test harness (test/)
# resolve like they do under pytest. Imports of the step's runtime-only deps
# (torch, transformers, ...) usually aren't installed in the dev venv, so
# import-not-found is suppressed. --follow-imports=silent keeps errors that live
# in src/gbserver/ out of the step report (staticcheck covers those).
# Under test/, redefined-outer-name is disabled: every pytest fixture used as a
# test argument triggers it, which buried real warnings (~2/3 of all messages).
check_step() {
    local root="$1" f
    local tests=() others=()
    shift
    for f in "$@"; do
        if [[ "${f}" == test/* ]]; then tests+=("${f}"); else others+=("${f}"); fi
    done
    echo "${MY_LINE_BREAK}"
    echo -e "\033[0;36mStep: \033[0m\033[0;32m${root}\033[0m"
    (
        cd "${REPO_ROOT}/${root}" || exit 1
        pylint_step_files "" "${others[@]+"${others[@]}"}"
        pylint_step_files "redefined-outer-name" "${tests[@]+"${tests[@]}"}"
        MYPYPATH="src:${REPO_ROOT}/src:${REPO_ROOT}/test" \
            mypy --explicit-package-bases --follow-imports=silent \
            --disable-error-code=import-untyped \
            --disable-error-code=import-not-found "$@"
    ) 2>&1 | sed -E "s#^([^ :]+\.py:)#${root}/\1#"
}

# Check every changed steps/ python file, grouped by step.
#   $@ - repo-relative changed file paths (non-steps paths are ignored)
check_steps() {
    local x root roots=() files
    for x in "$@"; do
        [[ "${x}" == steps/*.py && -f "${x}" ]] || continue
        root="$(step_root_of "${x}")"
        [[ -n "${root}" ]] || continue
        [[ " ${roots[*]-} " == *" ${root} "* ]] || roots+=("${root}")
    done
    for root in "${roots[@]+"${roots[@]}"}"; do
        files=()
        for x in "$@"; do
            [[ "${x}" == "${root}"/*.py && -f "${x}" ]] && files+=("${x#"${root}"/}")
        done
        check_step "${root}" "${files[@]}"
    done
}

# Grep the staticcheck output for each changed python file outside steps/.
#   $@ - repo-relative changed file paths
# staticcheck runs over all of src/gbserver/ and exits non-zero whenever any file
# there has an error, so make's "*** [staticcheck] Error 1" says nothing about the
# changed files. Its stderr is captured with the output and only the changed
# files' lines are shown. It is skipped entirely when no such file changed.
check_src() {
    local x output changed=()
    for x in "$@"; do
        # Skip paths that no longer exist on disk (deleted or renamed-away files
        # still appear in `git diff --name-only`); there's nothing to report for them.
        [[ -f "${x}" && "${x}" == *.py && "${x}" != steps/* ]] && changed+=("${x}")
    done
    [[ ${#changed[@]} -gt 0 ]] || return 0
    output="$(make --no-print-directory staticcheck 2>&1)"
    for x in "${changed[@]}"; do
        echo "${MY_LINE_BREAK}"
        echo -e "\033[0;36mFile: \033[0m\033[0;32m${x}\033[0m"
        echo
        echo "${output}" | grep "${x}"
    done
}

echo 'typecheck start'
cd "${REPO_ROOT}" || exit 1

# Collect the changed files, NUL-delimited. Read with `read -d ''` rather than
# `mapfile -d`, which needs bash 4 (macOS ships bash 3.2); see limited-format.sh.
difflist="$(mktemp)"
trap 'rm -f "${difflist}"' EXIT
if ! git diff main...HEAD --name-only -z --format= > "${difflist}"; then
    echo "ERROR: 'git diff main...HEAD' failed — cannot determine changed files." >&2
    exit 1
fi
files=()
while IFS= read -r -d '' x; do
    files+=("${x}")
done < "${difflist}"

check_src "${files[@]+"${files[@]}"}"
check_steps "${files[@]+"${files[@]}"}"

echo "${MY_LINE_BREAK}"
echo 'typecheck end'
