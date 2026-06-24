### Title
Unbounded Frame-Length Allocation in `FrameDecoder::decode` Allows Compromised Sandbox to OOM-Abort the Replica Controller Thread — (`rs/canister_sandbox/src/frame_decoder.rs`)

---

### Summary

`FrameDecoder::decode` reads a raw `u64` length field from the IPC socket and calls `data.reserve(size)` with no upper-bound check. A compromised sandbox process can send an 8-byte header containing `u64::MAX`, causing the replica's `CanisterSandboxIPC` thread to attempt an impossible allocation, which aborts the replica process.

---

### Finding Description

In `rs/canister_sandbox/src/frame_decoder.rs`, the `decode` method reads a `u64` from the wire, casts it directly to `usize`, and calls `data.reserve(size)` on a `BytesMut` buffer:

```rust
// frame_decoder.rs lines 40-47
let size = data.get_u64();
self.state = FrameDecoderState::Length(size);
// ...
let size: usize = *size as usize;
if data.len() < size {
    data.reserve(size);   // ← no upper-bound guard
    return None;
}
``` [1](#0-0) 

There is no `MAX_FRAME_SIZE` constant, no rejection of oversized lengths, and `SocketReaderConfig` only controls idle-timeout trimming — it imposes no message-size limit. [2](#0-1) 

`socket_read_messages` calls `decoder.decode(&mut buf)` in a tight loop with no interposed size check: [3](#0-2) 

The controller side spawns a dedicated `CanisterSandboxIPC` thread that runs this loop for every sandbox process: [4](#0-3) 

---

### Impact Explanation

`BytesMut::reserve(usize::MAX)` delegates to the global allocator. When the OS rejects the allocation, Rust's default allocator calls `handle_alloc_error`, which **aborts the process** (not a recoverable panic). Because the `CanisterSandboxIPC` thread runs inside the replica process, the entire replica process is terminated. This causes:

- Loss of the replica node from the subnet.
- If enough replicas are targeted simultaneously (one per canister sandbox), potential consensus gap or subnet stall.

---

### Likelihood Explanation

The precondition is a **compromised sandbox process** — i.e., a canister that has achieved arbitrary code execution inside its Wasmtime sandbox (e.g., via a Wasmtime CVE or memory-safety bug in the embedder). This is a non-trivial but realistic threat: the entire purpose of the sandbox isolation architecture is to contain exactly this scenario, and the IPC channel is the trust boundary that must be hardened against a malicious sandbox. Once the sandbox is compromised, sending 8 crafted bytes over the already-open `UnixStream` is trivial.

---

### Recommendation

Add a `MAX_FRAME_SIZE` constant and reject frames that exceed it before calling `reserve`:

```rust
const MAX_FRAME_SIZE: usize = 64 * 1024 * 1024; // e.g. 64 MiB

FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    if size > MAX_FRAME_SIZE {
        panic!("Frame size {} exceeds maximum allowed size", size);
        // or return an Err and close the socket gracefully
    }
    if data.len() < size {
        data.reserve(size);
        return None;
    }
    // ...
}
``` [5](#0-4) 

---

### Proof of Concept

```rust
use bytes::BytesMut;
use ic_canister_sandbox::frame_decoder::FrameDecoder;

// Craft a frame: 8-byte big-endian u64::MAX length, no payload
let mut buf = BytesMut::new();
buf.extend_from_slice(&u64::MAX.to_be_bytes());

let mut decoder = FrameDecoder::<Vec<u8>>::new();
// This call attempts data.reserve(usize::MAX) → allocator abort
decoder.decode(&mut buf);
```

Running this in the replica's `CanisterSandboxIPC` thread context terminates the replica process via `handle_alloc_error`. [6](#0-5)

### Citations

**File:** rs/canister_sandbox/src/frame_decoder.rs (L32-58)
```rust
    pub fn decode(&mut self, data: &mut BytesMut) -> Option<Message> {
        loop {
            match &self.state {
                FrameDecoderState::NoLength => {
                    if data.len() < 8 {
                        data.reserve(8);
                        return None;
                    } else {
                        let size = data.get_u64();
                        self.state = FrameDecoderState::Length(size);
                    }
                }
                FrameDecoderState::Length(size) => {
                    let size: usize = *size as usize;
                    if data.len() < size {
                        data.reserve(size);
                        return None;
                    } else {
                        let frame = data.split_to(size);
                        self.state = FrameDecoderState::NoLength;
                        let value = bincode::deserialize(&frame).unwrap();
                        return Some(value);
                    }
                }
            }
        }
    }
```

**File:** rs/canister_sandbox/src/transport.rs (L289-295)
```rust
pub struct SocketReaderConfig {
    // Specifies whether to call `libc::malloc_trim()` or not when
    // the socket becomes idle.
    idle_malloc_trim: bool,
    // Specifies the timeout after which the socket is considered idle.
    idle_timeout: Duration,
}
```

**File:** rs/canister_sandbox/src/transport.rs (L446-450)
```rust
    loop {
        while let Some(mut frame) = decoder.decode(&mut buf) {
            install_file_descriptors(&mut frame, &mut fds);
            handler(frame);
        }
```

**File:** rs/canister_sandbox/src/replica_controller/launch_as_process.rs (L115-138)
```rust
    let thread_handle = std::thread::Builder::new()
        .name("CanisterSandboxIPC".to_string())
        .spawn(move || {
            let demux = transport::Demux::<_, _, protocol::transport::SandboxToController>::new(
                Arc::new(rpc::ServerStub::new(
                    Arc::clone(&controller_service) as Arc<_>,
                    out.make_sink::<protocol::ctlsvc::Reply>(),
                )),
                reply_handler.clone(),
            );
            transport::socket_read_messages::<_, _>(
                move |message| {
                    demux.handle(message);
                },
                socket,
                SocketReaderConfig::default(),
            );
            reply_handler.flush_with_errors();
            controller_service.flush_with_errors();
            // Send a notification to the writer thread to stop.
            // Otherwise, the writer thread will remain waiting forever.
            out.stop();
        })
        .unwrap();
```
