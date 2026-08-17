Z80ASM ?= z80asm
PYTHON ?= python3
RELEASE_OUTPUT ?= dist
AGENT_SRC := agent/msx_agent.asm
AGENT_TSR_SRC := agent/msx_agent_tsr.asm
AGENT_CORE := agent/msx_agent_core.asm
AGENT_LOADER := agent/msx_memman_loader.asm
AGENT_XFER_SRC := agent/msx_xfer.asm
AGENT_XFER_ENGINE := agent/msx_xfer_engine.inc
AGENT_XFER_PROTOCOL := agent/msx_xfer_protocol.inc
AGENT_PORT_SRC := agent/msx_port_helper.asm
UNAPI_PROBE_SRC := agent/msx_unapi_probe.asm
AGENT_TRANSPORTS := agent/transports/msx_transport_8251.inc \
	agent/transports/msx_transport_16c550.inc \
	agent/transports/msx_transport_unapi.inc
AGENT_COM := work/agent/MSXAI.COM
AGENT_XFER_COM := work/agent/MSXAIXF.COM
AGENT_PORT_COM := work/agent/MP.COM
UNAPI_PROBE_COM := work/agent/UNAPIPRB.COM
AGENT_BUILD_DIR := work/agent/build
AGENT_TSR := $(AGENT_BUILD_DIR)/MSXAI.TSR
AGENT_TSR_METADATA := $(AGENT_BUILD_DIR)/MSXAI_TSR.INC
AGENT_TSR_8251 := work/agent/MCP8251.TSR
AGENT_TSR_16C550 := work/agent/MCP16550.TSR
AGENT_TSR_UNAPI := work/agent/MCPUNAPI.TSR
MEMMAN_VENDOR_DIR := work/agent
MEMMAN_VENDOR := $(MEMMAN_VENDOR_DIR)/MEMMAN.COM \
	$(MEMMAN_VENDOR_DIR)/TL.COM \
	$(MEMMAN_VENDOR_DIR)/TK.COM
AGENT_SUITE := $(AGENT_COM) $(AGENT_XFER_COM) \
	$(AGENT_TSR_8251) $(AGENT_TSR_16C550) $(AGENT_TSR_UNAPI) \
	$(AGENT_PORT_COM) $(MEMMAN_VENDOR)
# Bench MSX-DOS exposes a TPA from 0100h through 9898h (38,808 bytes).
# Keep 2 KiB free above each transient COM for its stack and DOS call headroom.
MSX_DOS_BENCH_COM_MAX := 36760
# MSXAIXF borrows 4000h-7FFFh as uninitialized accumulator RAM. Since COM
# images load at 0100h, the helper file itself must end no later than 3FFFh.
MSX_XFER_PAGE0_COM_MAX := 16128

.PHONY: agent agent-prerequisites agent-tsr memman-assets port-helper unapi-probe \
	unapi-emulation-preflight test test-integration test-unapi-emulation \
	release-check publish-check release-assets

agent: agent-prerequisites $(AGENT_COM) $(AGENT_XFER_COM) $(AGENT_PORT_COM)
	@test -s $(AGENT_COM)
	@test -s $(AGENT_XFER_COM)
	@test -s $(AGENT_PORT_COM)
	@test -s $(AGENT_TSR_8251)
	@test -s $(AGENT_TSR_16C550)
	@test -s $(AGENT_TSR_UNAPI)
	@test -s $(MEMMAN_VENDOR_DIR)/MEMMAN.COM
	@test -s $(MEMMAN_VENDOR_DIR)/TL.COM
	@test -s $(MEMMAN_VENDOR_DIR)/TK.COM

agent-prerequisites: memman-assets agent-tsr

memman-assets:
	"$(PYTHON)" tools/materialize_memman.py --output-dir $(MEMMAN_VENDOR_DIR)

agent-tsr:
	"$(PYTHON)" tools/build_agent_tsr.py --assembler "$(Z80ASM)" \
		--output $(AGENT_TSR) --metadata-output $(AGENT_TSR_METADATA) \
		--8251-output $(AGENT_TSR_8251) \
		--16c550-output $(AGENT_TSR_16C550) \
		--unapi-output $(AGENT_TSR_UNAPI)

port-helper: $(AGENT_PORT_COM)
	@test -s $(AGENT_PORT_COM)

$(AGENT_PORT_COM): $(AGENT_PORT_SRC) tools/build_port_helper.py
	"$(PYTHON)" tools/build_port_helper.py --assembler "$(Z80ASM)" \
		--source $(AGENT_PORT_SRC) --output $(AGENT_PORT_COM)

unapi-probe: $(UNAPI_PROBE_COM)
	@test -s $(UNAPI_PROBE_COM)

$(UNAPI_PROBE_COM): $(UNAPI_PROBE_SRC) tools/build_unapi_probe.py
	"$(PYTHON)" tools/build_unapi_probe.py --assembler "$(Z80ASM)" \
		--source $(UNAPI_PROBE_SRC) --output $(UNAPI_PROBE_COM)

# Prerequisites are intentionally normal (not order-only): generated metadata
# is assembled into the loader's external-suite validation, while the verified
# utilities and fixed-driver TSRs are deployable files in the same package.
$(AGENT_COM): agent-prerequisites $(AGENT_PORT_COM)
$(AGENT_COM): $(AGENT_SRC) $(AGENT_CORE) $(AGENT_LOADER) $(AGENT_XFER_PROTOCOL) $(AGENT_TRANSPORTS)
	mkdir -p $(dir $@)
	$(Z80ASM) $< -o $@
	"$(PYTHON)" tools/check_msx_com_size.py $@ $(MSX_DOS_BENCH_COM_MAX)

$(AGENT_XFER_COM): $(AGENT_XFER_SRC) $(AGENT_XFER_ENGINE) $(AGENT_XFER_PROTOCOL)
	mkdir -p $(dir $@)
	$(Z80ASM) $< -o $@
	"$(PYTHON)" tools/check_msx_com_size.py $@ $(MSX_XFER_PAGE0_COM_MAX)

test:
	"$(PYTHON)" -m unittest discover -s tests -v

test-integration:
	MSX_RUN_INTEGRATION=1 "$(PYTHON)" -m unittest tests.test_openmsx_resident -v

unapi-emulation-preflight:
	"$(PYTHON)" tools/openmsx_unapi_validation.py preflight

test-unapi-emulation:
	"$(PYTHON)" tools/openmsx_unapi_validation.py run

release-check:
	"$(PYTHON)" tools/release_check.py

publish-check:
	"$(PYTHON)" tools/release_check.py --publish

release-assets:
	"$(PYTHON)" tools/release_check.py --publish --output-dir "$(RELEASE_OUTPUT)"
