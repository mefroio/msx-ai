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
AGENT_VERSION_INCLUDE := agent/msx_version.inc
AGENT_PORT_SRC := agent/msx_port_helper.asm
AGENT_TU_SRC := agent/msx_tu_helper.asm
UNAPI_PROBE_SRC := agent/msx_unapi_probe.asm
BADCAT_INIT_SRC := agent/msx_badcat_init.asm
AGENT_TRANSPORTS := agent/transports/msx_transport_8251.inc \
	agent/transports/msx_transport_16c550.inc \
	agent/transports/msx_transport_fossil.inc \
	agent/transports/msx_transport_unapi.inc
AGENT_COM := work/agent/MSXAI.COM
AGENT_XFER_COM := work/agent/MSXAIXF.COM
AGENT_PORT_COM := work/agent/MP.COM
AGENT_TU_COM := work/agent/TU.COM
UNAPI_PROBE_COM := work/agent/UNAPIPRB.COM
BADCAT_INIT_COM := work/agent/BADINIT.COM
AGENT_BUILD_DIR := work/agent/build
AGENT_TSR := $(AGENT_BUILD_DIR)/MSXAI.TSR
AGENT_TSR_METADATA := $(AGENT_BUILD_DIR)/MSXAI_TSR.INC
AGENT_TSR_8251 := work/agent/MCP8251.TSR
AGENT_TSR_16C550 := work/agent/MCP16550.TSR
AGENT_TSR_16C550_115200 := work/agent/MCP115K.TSR
AGENT_TSR_UNAPI := work/agent/MCPUNAPI.TSR
AGENT_TRACE_SRC := agent/msx_agent_trace.asm
AGENT_TRACE_DIR := work/agent-trace
AGENT_TRACE_COM := $(AGENT_TRACE_DIR)/MSXAI.COM
AGENT_TRACE_XFER_COM := $(AGENT_TRACE_DIR)/MSXAIXF.COM
AGENT_TRACE_PORT_COM := $(AGENT_TRACE_DIR)/MP.COM
AGENT_TRACE_TU_COM := $(AGENT_TRACE_DIR)/TU.COM
AGENT_TRACE_BADCAT_INIT_COM := $(AGENT_TRACE_DIR)/BADINIT.COM
AGENT_TRACE_BUILD_DIR := $(AGENT_TRACE_DIR)/build
AGENT_TRACE_TSR := $(AGENT_TRACE_BUILD_DIR)/MSXAI.TSR
AGENT_TRACE_TSR_METADATA := $(AGENT_TRACE_BUILD_DIR)/MSXAI_TSR.INC
AGENT_TRACE_TSR_8251 := $(AGENT_TRACE_DIR)/MCP8251.TSR
AGENT_TRACE_TSR_16C550 := $(AGENT_TRACE_DIR)/MCP16550.TSR
AGENT_TRACE_TSR_16C550_115200 := $(AGENT_TRACE_DIR)/MCP115K.TSR
AGENT_TRACE_TSR_UNAPI := $(AGENT_TRACE_DIR)/MCPUNAPI.TSR
MEMMAN_VENDOR_DIR := work/agent
MEMMAN_VENDOR := $(MEMMAN_VENDOR_DIR)/MEMMAN.COM \
	$(MEMMAN_VENDOR_DIR)/TL.COM \
	$(MEMMAN_VENDOR_DIR)/TK.COM
AGENT_SUITE := $(AGENT_COM) $(AGENT_XFER_COM) \
	$(AGENT_TSR_8251) $(AGENT_TSR_16C550) $(AGENT_TSR_16C550_115200) \
	$(AGENT_TSR_UNAPI) $(AGENT_PORT_COM) $(AGENT_TU_COM) \
	$(BADCAT_INIT_COM) $(MEMMAN_VENDOR)
# Bench MSX-DOS exposes a TPA from 0100h through 9898h (38,808 bytes).
# Keep 2 KiB free above each transient COM for its stack and DOS call headroom.
MSX_DOS_BENCH_COM_MAX := 36760
# MSXAIXF borrows 4000h-7FFFh as uninitialized accumulator RAM. Since COM
# images load at 0100h, the helper file itself must end no later than 3FFFh.
MSX_XFER_PAGE0_COM_MAX := 16128

.PHONY: agent agent-prerequisites agent-tsr agent-trace agent-trace-prerequisites \
	agent-trace-tsr agent-trace-memman port-helper tu-helper unapi-probe badcat-init \
	unapi-emulation-preflight test test-integration test-unapi-emulation \
	release-check publish-check release-assets

agent: agent-prerequisites $(AGENT_COM) $(AGENT_XFER_COM) $(AGENT_PORT_COM) $(AGENT_TU_COM) $(BADCAT_INIT_COM)
	@test -s $(AGENT_COM)
	@test -s $(AGENT_XFER_COM)
	@test -s $(AGENT_PORT_COM)
	@test -s $(AGENT_TU_COM)
	@test -s $(BADCAT_INIT_COM)
	@test -s $(AGENT_TSR_8251)
	@test -s $(AGENT_TSR_16C550)
	@test -s $(AGENT_TSR_16C550_115200)
	@test -s $(AGENT_TSR_UNAPI)
	@test -s $(MEMMAN_VENDOR_DIR)/MEMMAN.COM
	@test -s $(MEMMAN_VENDOR_DIR)/TL.COM
	@test -s $(MEMMAN_VENDOR_DIR)/TK.COM

agent-prerequisites: memman-assets agent-tsr

agent-trace: agent-trace-prerequisites $(AGENT_TRACE_COM) \
		$(AGENT_TRACE_XFER_COM) $(AGENT_TRACE_PORT_COM) $(AGENT_TRACE_TU_COM) \
		$(AGENT_TRACE_BADCAT_INIT_COM)
	@test -s $(AGENT_TRACE_COM)
	@test -s $(AGENT_TRACE_XFER_COM)
	@test -s $(AGENT_TRACE_PORT_COM)
	@test -s $(AGENT_TRACE_TU_COM)
	@test -s $(AGENT_TRACE_BADCAT_INIT_COM)
	@test -s $(AGENT_TRACE_TSR_8251)
	@test -s $(AGENT_TRACE_TSR_16C550)
	@test -s $(AGENT_TRACE_TSR_16C550_115200)
	@test -s $(AGENT_TRACE_TSR_UNAPI)
	@test -s $(AGENT_TRACE_DIR)/MEMMAN.COM
	@test -s $(AGENT_TRACE_DIR)/TL.COM
	@test -s $(AGENT_TRACE_DIR)/TK.COM

agent-trace-prerequisites: agent-trace-memman agent-trace-tsr

agent-trace-memman:
	"$(PYTHON)" tools/materialize_memman.py --output-dir $(AGENT_TRACE_DIR)

agent-trace-tsr: server/_version.py tools/build_version_include.py
	"$(PYTHON)" tools/build_agent_tsr.py --assembler "$(Z80ASM)" \
		--development-trace \
		--output $(AGENT_TRACE_TSR) \
		--metadata-output $(AGENT_TRACE_TSR_METADATA) \
		--8251-output $(AGENT_TRACE_TSR_8251) \
		--16c550-output $(AGENT_TRACE_TSR_16C550) \
		--16c550-115200-output $(AGENT_TRACE_TSR_16C550_115200) \
		--unapi-output $(AGENT_TRACE_TSR_UNAPI)

memman-assets:
	"$(PYTHON)" tools/materialize_memman.py --output-dir $(MEMMAN_VENDOR_DIR)

agent-tsr: server/_version.py tools/build_version_include.py
	"$(PYTHON)" tools/build_agent_tsr.py --assembler "$(Z80ASM)" \
		--output $(AGENT_TSR) --metadata-output $(AGENT_TSR_METADATA) \
		--8251-output $(AGENT_TSR_8251) \
		--16c550-output $(AGENT_TSR_16C550) \
		--16c550-115200-output $(AGENT_TSR_16C550_115200) \
		--unapi-output $(AGENT_TSR_UNAPI)

port-helper: $(AGENT_PORT_COM)
	@test -s $(AGENT_PORT_COM)

$(AGENT_PORT_COM): $(AGENT_PORT_SRC) tools/build_port_helper.py
	"$(PYTHON)" tools/build_port_helper.py --assembler "$(Z80ASM)" \
		--source $(AGENT_PORT_SRC) --output $(AGENT_PORT_COM)

tu-helper: $(AGENT_TU_COM)
	@test -s $(AGENT_TU_COM)

$(AGENT_TU_COM): $(AGENT_TU_SRC) tools/build_tu_helper.py
	"$(PYTHON)" tools/build_tu_helper.py --assembler "$(Z80ASM)" \
		--source $(AGENT_TU_SRC) --output $(AGENT_TU_COM)

unapi-probe: $(UNAPI_PROBE_COM)
	@test -s $(UNAPI_PROBE_COM)

$(UNAPI_PROBE_COM): $(UNAPI_PROBE_SRC) tools/build_unapi_probe.py
	"$(PYTHON)" tools/build_unapi_probe.py --assembler "$(Z80ASM)" \
		--source $(UNAPI_PROBE_SRC) --output $(UNAPI_PROBE_COM)

badcat-init: $(BADCAT_INIT_COM)
	@test -s $(BADCAT_INIT_COM)

$(BADCAT_INIT_COM): $(BADCAT_INIT_SRC) tools/build_badcat_init.py
	"$(PYTHON)" tools/build_badcat_init.py --assembler "$(Z80ASM)" \
		--source $(BADCAT_INIT_SRC) --output $(BADCAT_INIT_COM)

# Prerequisites are intentionally normal (not order-only): generated metadata
# is assembled into the loader's external-suite validation, while the verified
# utilities and fixed-driver TSRs are deployable files in the same package.
$(AGENT_COM): agent-prerequisites $(AGENT_PORT_COM) $(AGENT_TU_COM)
$(AGENT_TRACE_COM): agent-trace-prerequisites $(AGENT_TRACE_PORT_COM) $(AGENT_TRACE_TU_COM)
$(AGENT_COM): AGENT_MAIN_SOURCE := $(AGENT_SRC)
$(AGENT_TRACE_COM): AGENT_MAIN_SOURCE := $(AGENT_TRACE_SRC)
$(AGENT_COM): $(AGENT_SRC)
$(AGENT_TRACE_COM): $(AGENT_TRACE_SRC)
$(AGENT_COM) $(AGENT_TRACE_COM): $(AGENT_CORE) $(AGENT_LOADER) $(AGENT_XFER_PROTOCOL) $(AGENT_VERSION_INCLUDE) $(AGENT_TRANSPORTS)
	mkdir -p $(dir $@)
	$(Z80ASM) $(AGENT_MAIN_SOURCE) -o $@
	"$(PYTHON)" tools/check_msx_com_size.py $@ $(MSX_DOS_BENCH_COM_MAX)

$(AGENT_XFER_COM): $(AGENT_XFER_SRC)
$(AGENT_TRACE_XFER_COM): $(AGENT_XFER_SRC)
$(AGENT_XFER_COM) $(AGENT_TRACE_XFER_COM): $(AGENT_XFER_ENGINE) $(AGENT_XFER_PROTOCOL)
	mkdir -p $(dir $@)
	$(Z80ASM) $(AGENT_XFER_SRC) -o $@
	"$(PYTHON)" tools/check_msx_com_size.py $@ $(MSX_XFER_PAGE0_COM_MAX)

$(AGENT_TRACE_PORT_COM): $(AGENT_PORT_SRC) tools/build_port_helper.py
	"$(PYTHON)" tools/build_port_helper.py --assembler "$(Z80ASM)" \
		--source $(AGENT_PORT_SRC) --output $(AGENT_TRACE_PORT_COM)

$(AGENT_TRACE_TU_COM): $(AGENT_TU_SRC) tools/build_tu_helper.py
	"$(PYTHON)" tools/build_tu_helper.py --assembler "$(Z80ASM)" \
		--source $(AGENT_TU_SRC) --output $(AGENT_TRACE_TU_COM)

$(AGENT_TRACE_BADCAT_INIT_COM): $(BADCAT_INIT_SRC) tools/build_badcat_init.py
	"$(PYTHON)" tools/build_badcat_init.py --assembler "$(Z80ASM)" \
		--source $(BADCAT_INIT_SRC) --output $(AGENT_TRACE_BADCAT_INIT_COM)

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
