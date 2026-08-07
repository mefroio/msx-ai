; Shared protocol-X constants for the resident and the transient MSXAIXF.COM.
; All multi-byte fields are little-endian. Encoding 1 is standard PackBits,
; decoded only for PUT; GET always remains byte-exact RAW.

TSR_TALK_XFER_CLAIM: equ 0A9h
TSR_TALK_XFER_READY: equ 0AAh
TSR_TALK_XFER_PUT_POLL: equ 0ABh
TSR_TALK_XFER_PUT_COMMIT: equ 0ACh
TSR_TALK_XFER_GET_PUBLISH: equ 0ADh
TSR_TALK_XFER_GET_POLL: equ 0AEh
TSR_TALK_XFER_FINISH: equ 0AFh
TSR_TALK_XFER_POSTPROCESS: equ 0B0h
TSR_TALK_XFER_PUT_RELEASE: equ 0B1h
TSR_TALK_XFER_PUMP: equ 0B2h

XFER_VERSION: equ 1
XFER_CMD_CAPS: equ 0
XFER_CMD_OPEN: equ 1
XFER_CMD_STATUS: equ 2
XFER_CMD_PUT_DATA: equ 3
XFER_CMD_GET_READ: equ 4
XFER_CMD_GET_ACK: equ 5
XFER_CMD_CLOSE: equ 6
XFER_CMD_CANCEL: equ 7
; Keep the proven fast-v1 negotiation opcodes byte-exact. Fast-v1 is the only
; active file-transfer data plane; FAST_PUMP remains an OPEN discriminator so
; obsolete clients fail closed instead of entering an incompatible state path.
XFER_CMD_FAST_CAPS: equ 8
XFER_CMD_FAST_BEGIN: equ 9
XFER_FAST_VERSION: equ 1
XFER_FAST_CAP_PUMP: equ 1
; Redesigned fast-v1 treats the successful framed response as the TCP-stream
; delivery boundary. PUT therefore streams the next block without a STATUS
; round-trip, while GET releases a transmitted block without a separate
; application GET_ACK. The final CRC-32 still covers the complete file.
XFER_FAST_CAP_STREAM: equ 2
XFER_FAST_CAPABILITIES: equ XFER_FAST_CAP_PUMP | XFER_FAST_CAP_STREAM
XFER_DIRECTION_PUT: equ 0
XFER_DIRECTION_GET: equ 1
XFER_ENCODING_RAW: equ 0
XFER_ENCODING_PACKBITS: equ 1
XFER_FLAG_RESUME: equ 1
; Host journaled a complete durable boundary before CLOSE. This is the only
; authorization to validate a final target after the helper removed its MTD.
XFER_FLAG_RECEIPTLESS_REPLAY: equ 2
XFER_FLAG_FAST_PUMP: equ 4
XFER_FLAGS_SUPPORTED: equ XFER_FLAG_RESUME | XFER_FLAG_RECEIPTLESS_REPLAY | XFER_FLAG_FAST_PUMP
XFER_CAP_RAW: equ 1
XFER_CAP_PUT: equ 2
XFER_CAP_GET: equ 4
XFER_CAP_RESUME: equ 8
XFER_CAP_CRC32: equ 16
XFER_CAP_PACKBITS_DECODE: equ 32
XFER_CAP_PACKBITS_ENCODE: equ 64
XFER_CAPABILITIES: equ XFER_CAP_RAW | XFER_CAP_PUT | XFER_CAP_GET | XFER_CAP_RESUME | XFER_CAP_CRC32 | XFER_CAP_PACKBITS_DECODE
XFER_PATH_MAX: equ 63
; Fast-v1 uses the largest payloads that fit a 2048-byte framed-v3 message.
XFER_FAST_PUT_CAPACITY: equ 2026
XFER_FAST_GET_CAPACITY: equ 2040
; The fast mailbox lives in the foreground helper's page-zero work area:
; length:u16 + prefix_crc32:u32 + the largest GET block.
XFER_FAST_WORK_CAPACITY: equ XFER_FAST_GET_CAPACITY + 6
; A second, adjacent helper workspace holds the complete pump-only v3 payload.
XFER_FAST_FRAME_CAPACITY: equ 2048
XFER_FAST_PAGE0_CAPACITY: equ XFER_FAST_WORK_CAPACITY + XFER_FAST_FRAME_CAPACITY
; Shared fast-v1 durability window. The transient helper owns the RAM; the
; resident uses this value only to constrain accepted-minus-durable credit.
XFER_FAST_ACCUMULATOR_CAPACITY: equ 16384
XFER_STATE_IDLE: equ 0
XFER_STATE_STAGED: equ 1
XFER_STATE_OPENING: equ 2
XFER_STATE_READY: equ 3
XFER_STATE_TRANSFERRING: equ 4
XFER_STATE_VERIFYING: equ 5
XFER_STATE_POSTPROCESS: equ 6
XFER_STATE_COMPLETE: equ 7
XFER_STATE_SUSPENDED: equ 8
XFER_STATE_FAILED: equ 9
XFER_STATE_CANCELLED: equ 10
XFER_ERROR_NONE: equ 0
XFER_ERROR_BINDING: equ 1
XFER_ERROR_IO: equ 2
XFER_ERROR_CRC: equ 3
XFER_ERROR_EXISTS: equ 4
XFER_ERROR_TIMEOUT: equ 5
XFER_ERROR_UNSUPPORTED: equ 6
XFER_ERROR_METADATA: equ 7
XFER_ERROR_RANGE: equ 8
XFER_STATUS_ACTIVE: equ 1
XFER_STATUS_RESUMABLE: equ 2
XFER_STATUS_WIRE_VERIFIED: equ 4
XFER_STATUS_FINAL_VERIFIED: equ 8
XFER_STATUS_PUBLISHED: equ 16

; Page-zero descriptor exchanged with the transient foreground process.
XFER_DESC_ID: equ 0
XFER_DESC_DIRECTION: equ 16
XFER_DESC_ENCODING: equ 17
XFER_DESC_FLAGS: equ 18
XFER_DESC_RESERVED: equ 19
XFER_DESC_WIRE_SIZE: equ 20
XFER_DESC_WIRE_CRC: equ 24
XFER_DESC_FINAL_SIZE: equ 28
XFER_DESC_FINAL_CRC: equ 32
XFER_DESC_RESUME_OFFSET: equ 36
XFER_DESC_PREFIX_CRC: equ 40
XFER_DESC_PATH_LENGTH: equ 44
XFER_DESC_PATH: equ 45
XFER_DESC_SIZE: equ XFER_DESC_PATH + XFER_PATH_MAX + 1
