#!/usr/bin/env bash
# Launch an interactive openMSX window using this repository's isolated setup.
# Additional arguments are passed directly to openMSX.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export OPENMSX_HOME="${OPENMSX_HOME:-${MSX_AI_OPENMSX_HOME:-$PROJECT_DIR/.openmsx-home}}"

find_openmsx() {
  if [[ -n "${OPENMSX_BIN:-}" ]]; then
    if [[ ! -x "$OPENMSX_BIN" ]]; then
      echo "OPENMSX_BIN is not executable: $OPENMSX_BIN" >&2
      return 1
    fi
    printf '%s\n' "$OPENMSX_BIN"
    return
  fi

  if command -v openmsx >/dev/null 2>&1; then
    command -v openmsx
    return
  fi

  local macos_binary="/Applications/openMSX.app/Contents/MacOS/openmsx"
  if [[ -x "$macos_binary" ]]; then
    printf '%s\n' "$macos_binary"
    return
  fi

  echo "openMSX was not found. Install it or set OPENMSX_BIN to its executable." >&2
  return 1
}

usage() {
  cat <<'EOF'
Usage: ./open-msx.command [basic|disk|dos|msx2plus] [openMSX arguments...]

Environment overrides:
  OPENMSX_BIN                 Path to the openMSX executable
  OPENMSX_HOME                openMSX user directory
  MSX_AI_OPENMSX_HOME         Project-specific alias for OPENMSX_HOME
  MSX_AI_BASIC_MACHINE        Machine used by basic, disk, and dos profiles
  MSX_AI_MSX2PLUS_MACHINE     Machine used by the msx2plus profile
  MSX_AI_DISK_EXTENSION       Disk extension used by the disk profile
  MSX_AI_DOS_EXTENSION        IDE extension used by the dos profile
  MSX_AI_DOS_HDD              Hard-disk image used by the dos profile
EOF
}

PROFILE="${1:-basic}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$PROFILE" in
  -h|--help|help)
    usage
    exit 0
    ;;
  basic|disk|dos|msx2plus)
    ;;
  *)
    echo "Unknown profile: $PROFILE" >&2
    usage >&2
    exit 2
    ;;
esac

OPENMSX_EXECUTABLE="$(find_openmsx)"
BASIC_MACHINE="${MSX_AI_BASIC_MACHINE:-Gradiente_Expert20}"
MSX2PLUS_MACHINE="${MSX_AI_MSX2PLUS_MACHINE:-Sony_HB-F1XDJ_128K_Lite}"
DISK_EXTENSION="${MSX_AI_DISK_EXTENSION:-DDX_3.0}"
DOS_EXTENSION="${MSX_AI_DOS_EXTENSION:-SunriseIDE_Nextor}"
DOS_DISK="${MSX_AI_DOS_HDD:-$PROJECT_DIR/work/system-disks/msxdos.dsk}"

openmsx_args=()
case "$PROFILE" in
  basic)
    openmsx_args=(-machine "$BASIC_MACHINE")
    ;;
  disk)
    openmsx_args=(-machine "$BASIC_MACHINE" -ext "$DISK_EXTENSION")
    ;;
  dos)
    if [[ ! -f "$DOS_DISK" ]]; then
      echo "DOS disk image not found: $DOS_DISK" >&2
      echo "Set MSX_AI_DOS_HDD to an existing image." >&2
      exit 1
    fi
    openmsx_args=(-machine "$BASIC_MACHINE" -ext "$DOS_EXTENSION" -hda "$DOS_DISK")
    ;;
  msx2plus)
    openmsx_args=(-machine "$MSX2PLUS_MACHINE")
    ;;
esac

LOCAL_SETTINGS="$OPENMSX_HOME/share/settings.local.xml"
if [[ -f "$LOCAL_SETTINGS" ]]; then
  openmsx_args+=(-setting "$LOCAL_SETTINGS")
fi

echo "Starting openMSX (profile: $PROFILE, machine: ${openmsx_args[1]})"
exec "$OPENMSX_EXECUTABLE" "${openmsx_args[@]}" "$@"
