#!/usr/bin/env bash
#
# Install the AICC Builder skill into the local Claude Code or Kiro
# skills directory, renaming claude/ or kiro/ to aicc-builder/ so the
# enclosing folder name matches the SKILL.md 'name:' frontmatter.
#
# Usage:
#   install.sh claude user       # -> ~/.claude/skills/aicc-builder/
#   install.sh claude project    # -> ./.claude/skills/aicc-builder/
#   install.sh kiro user         # -> ~/.kiro/skills/aicc-builder/
#   install.sh kiro project      # -> ./.kiro/skills/aicc-builder/
#
set -euo pipefail

PLATFORM="${1:-}"
SCOPE="${2:-user}"

if [[ "${PLATFORM}" != "claude" && "${PLATFORM}" != "kiro" ]]; then
    echo "Usage: $0 {claude|kiro} [user|project]" >&2
    exit 2
fi

case "${SCOPE}" in
    user)    DEST_ROOT="${HOME}/.${PLATFORM}/skills" ;;
    project) DEST_ROOT="./.${PLATFORM}/skills" ;;
    *) echo "scope must be 'user' or 'project'" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${HERE}/.." && pwd)"
DEST="${DEST_ROOT}/aicc-builder"

mkdir -p "${DEST_ROOT}"
if [[ -d "${DEST}" ]]; then
    echo "Removing existing install at ${DEST}"
    rm -rf "${DEST}"
fi

cp -r "${SRC}" "${DEST}"
mv "${DEST}/${PLATFORM}/SKILL.md" "${DEST}/SKILL.md"

# Remove the sibling platform's directory and the dual-platform helper dirs
rm -rf "${DEST}/claude" "${DEST}/kiro" "${DEST}/scripts"
# Keep resources/; drop the top-level README (skill-internal README lives at SKILL.md)
rm -f "${DEST}/README.md"

echo ""
echo "✓ Installed aicc-builder skill to ${DEST}"
echo ""
echo "Verify: ls ${DEST}/"
if [[ "${PLATFORM}" == "claude" ]]; then
    echo "In Claude Code, run '/skills' and confirm 'aicc-builder' is listed."
else
    echo "In Kiro, restart the editor (or reload skills) and confirm 'aicc-builder' is listed."
fi
