#!/usr/bin/env bash
# Rewrite shebangs in <target>/bin/ to point at <target>/bin/python3.12.
#
# pip bakes the absolute path of its interpreter into every console-script
# shebang when it installs a package. When the bundled python tree is moved
# (repo → /Applications copy at build time), pip's baked-in absolute shebangs
# go stale and every script fails with ENOENT. This script
# walks <target>/bin/ once and rewrites any shebang ending in
# `/python/bin/pythonX[.Y]` to use the python sitting next to the script.
#
# Usage:
#   scripts/fix_python_shebangs.sh <python-root>
#     where <python-root> contains bin/python3.12
set -euo pipefail

target="${1:?usage: $0 <python-root>}"
target_abs="$(cd "$target" && pwd)"

# Where the shebang should point to. By default this is the python interpreter
# inside the target tree itself (the dev-repo case). When INSTALL_ROOT is
# exported, shebangs are rewritten to point at that path instead — used at
# build time so the bundle's scripts reference their eventual install location
# (e.g. /Applications/Estormi.app/Contents/Resources/_up_/_up_/python).
shebang_root="${INSTALL_ROOT:-$target_abs}"
py="$shebang_root/bin/python3.12"

if [ -z "${INSTALL_ROOT:-}" ] && [ ! -x "$py" ]; then
  echo "fix_python_shebangs: $py is not executable — aborting" >&2
  exit 1
fi

want="#!$py"
n_fixed=0

for f in "$target_abs"/bin/*; do
  [ -f "$f" ] || continue
  # Read the first line. Skip binary files (no leading shebang or non-UTF lines).
  IFS= read -r first <"$f" 2>/dev/null || continue
  case "$first" in
    "#!"*python*)
      if [ "$first" = "$want" ]; then
        continue
      fi
      # Only rewrite when the shebang points at a python interpreter — leaves
      # `#!/bin/sh` wrappers and other non-python shebangs untouched.
      case "$first" in
        *"/python3"* | *"/python "* | *"/python")
          tmp="$f.shebang.tmp"
          { printf '%s\n' "$want"; tail -n +2 "$f"; } >"$tmp"
          mode=$(stat -f '%Lp' "$f" 2>/dev/null || stat -c '%a' "$f")
          chmod "$mode" "$tmp"
          mv "$tmp" "$f"
          n_fixed=$((n_fixed + 1))
          ;;
      esac
      ;;
  esac
done

echo "fix_python_shebangs: rewrote $n_fixed script(s) under $target_abs/bin/"
