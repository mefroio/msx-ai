# Resumable MSX-DOS file transfer

`msx_file_put` and `msx_file_get` transfer arbitrary files through a current
resident agent and the matching `MSXAIXF.COM` transient helper. Direct openMSX
and foreground-monitor sessions do not provide this data plane.

## Starting a transfer

The MSX must be at a confirmed DOS prompt before a new transfer starts. Set
`dos_prompt_confirmed=true` only after confirming that state externally. The
tool deliberately does not capture VRAM to infer the prompt. With confirmation
false, a request may only recover an already-active matching transfer when
resume is enabled.

A transfer is bound to a random 16-byte identifier and an immutable descriptor
containing direction, DOS path, wire and final sizes, checksums, encoding, and
resume boundary. The host then starts `MSXAIXF /PUT` or `MSXAIXF /GET` with
that identifier. All DOS calls execute in the foreground helper; the resident
hook performs only bounded framing and mailbox copies.

## Integrity and publication

The protocol uses 32-bit sizes and offsets, framed CRC-16 for transport
messages, and end-to-end CRC-32 for file content. Verified durable checkpoints
allow restart after a timeout, TCP disconnect, or host restart. A completed
upload is published only after size, checksum, close, and final-name checks.
An incomplete target is never reported as a successful file.

Downloads use collision-safe host publication and do not overwrite an existing
destination. If the connection is lost beyond the last durable checkpoint,
unverified bytes are discarded and requested again.

Final GET publication uses a hard link so that “does not exist” and “publish
the already-verified bytes” remain one atomic, no-overwrite operation. The
destination filesystem must therefore support hard links; FAT and exFAT are
not suitable destinations. If the host rejects the link, MSX-AI fails closed
and preserves the verified `.msxpart` file and journal. Select a destination on
a hard-link-capable local filesystem and repeat the same resumable GET.

## Fast data plane and encoding

The required `fast-v1` path moves near-2 KiB sequential frames through a
transient 16 KiB disk-I/O accumulator. PUT avoids a separate status exchange
for every data frame; GET records sparse durable acknowledgements. The final
result reports stream bytes, elapsed seconds, and measured bytes per second.

When the MCP client supplies a progress token, PUT reports accepted bytes and
the latest durable boundary, while GET reports received bytes and its latest
fsync-backed checkpoint. MCP cancellation sends a best-effort protocol CANCEL
at a safe frame boundary and waits for the synchronous target worker to clean
up before another hardware operation may start. Journals and partial files are
retained, so repeating the same operation with resume enabled revalidates and
continues rather than publishing an incomplete file.

`compression="auto"` applies bounded PackBits encoding only when it reduces
wire size and the target advertises the decoder. Already-compressed inputs stay
byte-exact. `compression="raw"` disables transport encoding. GET is currently
raw because no matching MSX-side encoder is negotiated.

An unambiguously textual BASIC upload is normalized to MSX-DOS line endings
and an end marker before checksums are calculated. Tokenized BASIC and other
binary files remain byte-exact.

## Recovery rule

Keep `resume=true` unless deliberately abandoning recovery. After a failure,
repeat the same direction, source and destination identity. Do not replace the
agent suite while a transfer is active. Files copied from a newer build do not
update a TSR already resident in memory; uninstall and reinstall the matching
suite before starting a new session.
