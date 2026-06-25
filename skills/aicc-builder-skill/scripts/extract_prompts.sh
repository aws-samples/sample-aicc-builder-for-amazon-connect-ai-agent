#!/usr/bin/env bash
#
# Re-extract prompts and JSON schemas from the ECS backend into the
# skill's resources/ tree. Run this after editing any Python-string
# prompt in backend/ecs/src/ so the skill stays in sync.
#
# Modes:
#   extract_prompts.sh            Re-extract in place.
#   extract_prompts.sh --check    Extract into a tempdir and diff against
#                                 resources/. Exit 0 if identical, 1 if
#                                 drift is detected. Intended for CI /
#                                 pre-commit to prevent silent skill
#                                 degradation when a .py prompt is edited
#                                 without re-extraction.
#
# Requires: Python 3.9+, Pydantic v2.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "${HERE}/.." && pwd)"
REPO_ROOT="$(cd "${SKILL_ROOT}/../.." && pwd)"

MODE="${1:-extract}"

case "${MODE}" in
  extract)
    echo "Skill root:  ${SKILL_ROOT}"
    echo "Repo root:   ${REPO_ROOT}"
    echo ""
    python3 "${HERE}/_extract_prompts.py" "${REPO_ROOT}" "${SKILL_ROOT}"
    echo ""
    echo "✓ resources/orchestrator/*.md"
    echo "✓ resources/sub-agents/*.md"
    echo "✓ resources/schemas/*.json"
    echo ""
    echo "Next: review 'git diff skills/aicc-builder-skill/resources/' and commit."
    ;;
  --check|check)
    TMP_ROOT="$(mktemp -d -t aicc-skill-check-XXXXXX)"
    trap 'rm -rf "${TMP_ROOT}"' EXIT

    # Stage a minimal skill root with the subdirs the extractor writes into.
    mkdir -p "${TMP_ROOT}/resources/orchestrator"
    mkdir -p "${TMP_ROOT}/resources/sub-agents"
    mkdir -p "${TMP_ROOT}/resources/schemas"

    # --strict: a coverage gap (a new backend prompt section / spec model the
    # extractor doesn't enumerate) is a hard failure here, not just a warning —
    # otherwise the self-diff below would report "no drift" for something that
    # never reached resources/ in the first place.
    if ! python3 "${HERE}/_extract_prompts.py" --strict "${REPO_ROOT}" "${TMP_ROOT}" >"${TMP_ROOT}/extract.log" 2>&1; then
      echo "Coverage gap: the extractor does not cover all backend prompt sections / spec models." >&2
      grep -A 50 "COVERAGE GAP" "${TMP_ROOT}/extract.log" >&2 || cat "${TMP_ROOT}/extract.log" >&2
      echo "" >&2
      echo "Add the missing items to _extract_prompts.py (phases / standalone_orch /" >&2
      echo "schema_models) or to the *_COVERAGE_IGNORE sets, then re-run." >&2
      exit 1
    fi

    DRIFT=0
    for sub in orchestrator sub-agents schemas; do
      if ! diff -r -q \
          "${SKILL_ROOT}/resources/${sub}" \
          "${TMP_ROOT}/resources/${sub}" >/dev/null 2>&1; then
        DRIFT=1
        echo "DRIFT in resources/${sub}/:"
        diff -r "${SKILL_ROOT}/resources/${sub}" "${TMP_ROOT}/resources/${sub}" || true
        echo ""
      fi
    done

    if [[ "${DRIFT}" -ne 0 ]]; then
      echo "Skill prompts are out of sync with backend/ecs/src/." >&2
      echo "Run: skills/aicc-builder-skill/scripts/extract_prompts.sh" >&2
      exit 1
    fi

    echo "✓ Skill prompts match backend/ecs/src/ (no drift)."
    ;;
  *)
    echo "Usage: $0 [--check]" >&2
    exit 2
    ;;
esac
