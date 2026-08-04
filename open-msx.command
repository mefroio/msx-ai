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
Usage: ./open-msx.command [basic|disk|dos|msx2plus|mcp] [openMSX arguments...]

Environment overrides:
  OPENMSX_BIN                 Path to the openMSX executable
  OPENMSX_HOME                openMSX user directory
  MSX_AI_OPENMSX_HOME         Project-specific alias for OPENMSX_HOME
  MSX_AI_BASIC_MACHINE        Machine used by basic, disk, and dos profiles
  MSX_AI_MSX2PLUS_MACHINE     Machine used by the msx2plus profile
  MSX_AI_DISK_EXTENSION       Disk extension used by the disk profile
  MSX_AI_DOS_EXTENSION        IDE extension used by the dos profile
  MSX_AI_DOS_HDD              Hard-disk image used by the dos profile
  MSX_AI_MCP_SLOT_EXPANDER    Slot expander used by the MCP profile (default: slotexpander)
  MSX_AI_MCP_IPV4             MCP listener IPv4 address (default: 127.0.0.1)
  MSX_AI_MCP_PORT             MCP listener TCP port (default: 6603)
EOF
}

validate_ipv4() {
  local address="$1"
  local octet
  local -a octets
  if [[ ! "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "Invalid IPv4 address: $address" >&2
    return 1
  fi
  IFS='.' read -r -a octets <<< "$address"
  for octet in "${octets[@]}"; do
    if (( 10#$octet > 255 )); then
      echo "Invalid IPv4 address: $address" >&2
      return 1
    fi
  done
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
  basic|disk|dos|msx2plus|mcp)
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
MCP_SLOT_EXPANDER="${MSX_AI_MCP_SLOT_EXPANDER:-slotexpander}"
MCP_RUNTIME_DIR=""
MCP_SETTINGS=""
MCP_DISK=""
MCP_SCRIPT=""
MCP_RUNTIME_AGENT=""

cleanup_mcp_runtime() {
  if [[ -n "$MCP_RUNTIME_DIR" ]]; then
    rm -f "$MCP_DISK" "$MCP_SETTINGS" "$MCP_SCRIPT" "$MCP_RUNTIME_AGENT"
    rmdir "$MCP_RUNTIME_DIR" 2>/dev/null || true
  fi
}

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
  mcp)
    if [[ ! -f "$DOS_DISK" ]]; then
      echo "DOS disk image not found: $DOS_DISK" >&2
      echo "Set MSX_AI_DOS_HDD to an existing image." >&2
      exit 1
    fi
    if command -v pgrep >/dev/null 2>&1 && pgrep -x openmsx >/dev/null 2>&1; then
      echo "An openMSX process is already running; refusing to start a second one." >&2
      exit 1
    fi

    MCP_IPV4="${MSX_AI_MCP_IPV4:-127.0.0.1}"
    MCP_PORT_TEXT="${MSX_AI_MCP_PORT:-6603}"
    validate_ipv4 "$MCP_IPV4"
    if [[ ! "$MCP_PORT_TEXT" =~ ^[0-9]+$ ]]; then
      echo "Invalid MCP TCP port: $MCP_PORT_TEXT" >&2
      exit 1
    fi
    MCP_PORT_VALUE=$((10#$MCP_PORT_TEXT))
    if (( MCP_PORT_VALUE < 1 || MCP_PORT_VALUE > 65535 )); then
      echo "Invalid MCP TCP port: $MCP_PORT_TEXT" >&2
      exit 1
    fi

    make -C "$PROJECT_DIR" agent >/dev/null
    MCP_AGENT_COM="$PROJECT_DIR/work/agent/MSXAI.COM"
    if [[ ! -s "$MCP_AGENT_COM" ]]; then
      echo "Agent build did not produce: $MCP_AGENT_COM" >&2
      exit 1
    fi

    MCP_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/msx-ai-mcp.XXXXXX")"
    MCP_DISK="$MCP_RUNTIME_DIR/msxdos.dsk"
    MCP_SETTINGS="$MCP_RUNTIME_DIR/settings.xml"
    MCP_SCRIPT="$MCP_RUNTIME_DIR/startup.tcl"
    MCP_RUNTIME_AGENT="$MCP_RUNTIME_DIR/MSXAI.COM"
    trap cleanup_mcp_runtime EXIT INT TERM
    cp "$DOS_DISK" "$MCP_DISK"
    cp "$PROJECT_DIR/tools/openmsx_mcp_test.tcl" "$MCP_SCRIPT"
    cp "$MCP_AGENT_COM" "$MCP_RUNTIME_AGENT"

    export MSX_AI_MCP_AGENT_COM="$MCP_RUNTIME_AGENT"
    export MSX_AI_MCP_IPV4="$MCP_IPV4"
    export MSX_AI_MCP_PORT="$MCP_PORT_VALUE"
    # Insert the expander first so four secondary slots are available before
    # the required MCP/IDE cartridges and any user hardware are added.
    openmsx_args=(-machine "$BASIC_MACHINE"
                  -ext "$MCP_SLOT_EXPANDER"
                  -ext "$DOS_EXTENSION"
                  -ext rs232_proto
                  -hda "$MCP_DISK")
    ;;
esac

LOCAL_SETTINGS="$OPENMSX_HOME/share/settings.local.xml"
if [[ "$PROFILE" == "mcp" ]]; then
  if [[ -f "$LOCAL_SETTINGS" ]]; then
    cp "$LOCAL_SETTINGS" "$MCP_SETTINGS"
  else
    cp "$OPENMSX_HOME/share/settings.xml" "$MCP_SETTINGS"
  fi
  openmsx_args+=(-setting "$MCP_SETTINGS"
                 -script "$MCP_SCRIPT")
elif [[ -f "$LOCAL_SETTINGS" ]]; then
  openmsx_args+=(-setting "$LOCAL_SETTINGS")
fi

echo "Starting openMSX (profile: $PROFILE, machine: ${openmsx_args[1]})"
if [[ "$PROFILE" == "mcp" ]]; then
  echo "MSXAI.COM is available for manual start at the DOS prompt"
  echo "Waiting for MCP TCP listener at $MSX_AI_MCP_IPV4:$MSX_AI_MCP_PORT"
  "$OPENMSX_EXECUTABLE" "${openmsx_args[@]}" "$@"
  exit $?
fi
exec "$OPENMSX_EXECUTABLE" "${openmsx_args[@]}" "$@"
