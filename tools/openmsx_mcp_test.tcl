# User-owned openMSX instance for MSX-AI MCP TCP development.
# The launcher supplies a disposable DOS disk and these validated variables:
# MSX_AI_MCP_AGENT_COM, MSX_AI_MCP_IPV4, and MSX_AI_MCP_PORT.

namespace eval msx_ai_mcp {
    variable retry_seconds 1.0

    proc required_environment {name} {
        if {![info exists ::env($name)] || $::env($name) eq ""} {
            error "required environment variable $name is missing"
        }
        return $::env($name)
    }

    proc retry_connection {} {
        variable retry_seconds
        catch {unplug msx-rs232}
        if {[catch {plug msx-rs232 rs232-net} reason]} {
            after realtime $retry_seconds [namespace code retry_connection]
            return
        }
        puts "MSX-AI: TCP transport connected to [set rs232-net-address]"
    }

    proc reconnect {} {
        catch {unplug msx-rs232}
        after realtime 0 [namespace code retry_connection]
        puts "MSX-AI: TCP reconnect armed"
    }

    proc agent_ready {} {
        set throttle on
        set mute off
        set renderer SDLGL-PP
        after realtime 0 [namespace code retry_connection]
        puts "MSX-AI: foreground DEBUG monitor ready; press F11 to reconnect"
    }

    proc launch_agent {} {
        //type "MSXAI /DRIVER:8251 /MONITOR DEBUG ON\r"
        after time 1 [namespace code agent_ready]
    }

    proc start {} {
        set agent [required_environment MSX_AI_MCP_AGENT_COM]
        set ipv4 [required_environment MSX_AI_MCP_IPV4]
        set port [required_environment MSX_AI_MCP_PORT]

        set power off
        set listing [diskmanipulator dir hda1]
        if {[string match -nocase "*msxai.com*" $listing]} {
            diskmanipulator delete hda1 MSXAI.COM
        }
        diskmanipulator import hda1 $agent

        set rs232-net-address "$ipv4:$port"
        set rs232-net-ip232 off
        set default_type_proc type_via_keybuf
        set renderer none
        set mute on
        set throttle off
        set power on
        after time 14 [namespace code launch_agent]
    }
}

bind "keyb F11" {msx_ai_mcp::reconnect}
msx_ai_mcp::start
