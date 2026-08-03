Z80ASM ?= z80asm
PYTHON ?= python3
AGENT_SRC := agent/msx_agent.asm
AGENT_TSR_SRC := agent/msx_agent_tsr.asm
AGENT_CORE := agent/msx_agent_core.asm
AGENT_LOADER := agent/msx_memman_loader.asm
AGENT_TRANSPORTS := agent/transports/msx_transport_8251.inc \
	agent/transports/msx_transport_16c550.inc
AGENT_COM := work/agent/MSXAI.COM
AGENT_TSR := work/agent/MSXAI.TSR
AGENT_TSR_METADATA := work/agent/MSXAI_TSR.INC
MEMMAN_VENDOR_DIR := work/agent/vendor
MEMMAN_VENDOR := $(MEMMAN_VENDOR_DIR)/MEMMAN.COM \
	$(MEMMAN_VENDOR_DIR)/TL.COM \
	$(MEMMAN_VENDOR_DIR)/TK.COM

.PHONY: agent agent-prerequisites agent-tsr memman-assets test test-integration

agent: $(AGENT_COM)

agent-prerequisites: memman-assets agent-tsr

memman-assets:
	$(PYTHON) tools/materialize_memman.py --output-dir $(MEMMAN_VENDOR_DIR)

agent-tsr:
	$(PYTHON) tools/build_agent_tsr.py --assembler "$(Z80ASM)" \
		--output $(AGENT_TSR) --metadata-output $(AGENT_TSR_METADATA)

# Prerequisites are intentionally normal (not order-only): the generated TSR
# and verified vendor bytes are embedded in the COM, so every regeneration must
# force the final assembly as well.
$(AGENT_COM): agent-prerequisites
$(AGENT_COM): $(AGENT_SRC) $(AGENT_CORE) $(AGENT_LOADER) $(AGENT_TRANSPORTS)
	mkdir -p $(dir $@)
	$(Z80ASM) $< -o $@

test:
	$(PYTHON) -m unittest discover -s tests -v

test-integration:
	MSX_RUN_INTEGRATION=1 $(PYTHON) -m unittest tests.test_openmsx_resident -v
