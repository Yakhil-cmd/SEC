### Title
Unbounded Memory Allocation from Attacker-Controlled Length Field in Canister Sandbox IPC Frame Decoder - (`rs/canister_sandbox/src/frame_decoder.rs`)

### Summary

The `FrameDecoder` used for canister sandbox IPC reads a raw `u64` length field from the wire and calls `data.reserve(size)` without any upper-bound validation. A compromised sandbox process (reachable via a malicious canister that achieves a Wasm sandbox escape) can send a single crafted frame with a faked length of up to `u64::MAX`, causing the replica controller process to attempt an unbounded heap allocation and crash with OOM — taking down the entire subnet node.

### Finding Description

`rs/canister_sandbox/src/frame_decoder.rs` implements a length-prefixed frame protocol for the Unix-domain-socket IPC channel between the replica controller and each per-canister sandbox process:

```rust
FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    if data.len() < size {
        data.reserve(size);   // ← unbounded allocation from wire-supplied u64
        return None;
    } else { ... }
}
``` [1](#0-0) 

The 8-byte length prefix is written by the sender in `write_frame`:

```rust
state.buf.put_u64(data.len() as u64);
``` [2](#0-1) 

`socket_read_messages` drives this decoder on the **controller (replica) side** when reading responses from the sandbox process: [3](#0-2) 

The IC project itself has already identified this exact pattern as dangerous and banned `bincode::deserialize_from` in `clippy.toml` for the identical reason — it reads a `u64` length and allocates without validation: [4](#0-3) 

`FrameDecoder` re-implements the same unsafe pattern manually and is not covered by that lint.

### Impact Explanation

If a malicious canister achieves a Wasm sandbox escape (a separate vulnerability class), it gains code execution inside the sandbox OS process. From there it can write a single 8-byte frame to the Unix socket with a length field of, e.g., `0xFFFFFFFFFFFFFFFF`. The replica controller's `FrameDecoder` will call `data.reserve(0xFFFFFFFFFFFFFFFF)`, which will either:

- Immediately panic/abort the replica process (Rust's allocator panics on allocation failure by default), or
- Trigger the Linux OOM killer against the replica process.

Either outcome terminates the replica on that node, causing a denial-of-service for all canisters hosted on that subnet node. If multiple nodes are targeted simultaneously (each with their own malicious canister), subnet liveness can be disrupted below the fault-tolerance threshold.

### Likelihood Explanation

The attack requires two steps: (1) a Wasm sandbox escape and (2) exploitation of this `FrameDecoder` bug. Step 1 is a separate, non-trivial vulnerability. However, the `FrameDecoder` bug is a force-multiplier: any future sandbox escape automatically yields a reliable replica-crash primitive. The IC team's own `clippy.toml` demonstrates awareness that this length-without-validation pattern is dangerous; the `FrameDecoder` is an unguarded instance of the same pattern in a security-critical IPC path.

### Recommendation

Add a maximum frame size constant and reject frames that exceed it before calling `reserve`:

```rust
const MAX_FRAME_SIZE: usize = 128 * 1024 * 1024; // e.g. 128 MiB

FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    if size > MAX_FRAME_SIZE {
        panic!("IPC frame size {} exceeds maximum {}", size, MAX_FRAME_SIZE);
    }
    if data.len() < size {
        data.reserve(size);
        return None;
    }
    ...
}
``` [5](#0-4) 

The bound should be chosen to be larger than any legitimate IPC message (the largest legitimate messages are Wasm module uploads and execution state snapshots) but small enough to prevent OOM.

### Proof of Concept

1. A malicious canister achieves Wasm sandbox escape (separate step).
2. From within the sandbox process, write 8 bytes to the controller Unix socket: `\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF` (length = `u64::MAX`).
3. The controller's `socket_read_messages` loop calls `decoder.decode(&mut buf)`.
4. `FrameDecoder` reads the 8-byte length, enters `FrameDecoderState::Length(u64::MAX)`, and calls `data.reserve(usize::MAX)`.
5. The Rust allocator panics (or the OS OOM-kills the replica process).
6. The replica node crashes; the subnet loses one node. [6](#0-5) [7](#0-6)

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

**File:** rs/canister_sandbox/src/transport.rs (L206-206)
```rust
        state.buf.put_u64(data.len() as u64);
```

**File:** rs/canister_sandbox/src/transport.rs (L434-484)
```rust
pub fn socket_read_messages<
    Message: DeserializeOwned + EnumerateInnerFileDescriptors + Clone,
    Handler: Fn(Message),
>(
    handler: Handler,
    socket: Arc<UnixStream>,
    config: SocketReaderConfig,
) {
    let mut decoder = FrameDecoder::<Message>::new();
    let mut buf = BytesMut::with_capacity(INITIAL_BUFFER_CAPACITY);
    let mut fds = Vec::<RawFd>::new();
    let mut reader = SocketReaderWithTimeout::new(socket);
    loop {
        while let Some(mut frame) = decoder.decode(&mut buf) {
            install_file_descriptors(&mut frame, &mut fds);
            handler(frame);
        }

        let num_bytes_received =
            match reader.receive_message(&mut buf, &mut fds, 0, Some(config.idle_timeout)) {
                Some(bytes) => bytes,
                None => {
                    // The operation has timed out.
                    // Trim the buffer and trim malloc if needed.
                    if buf.is_empty() {
                        buf = BytesMut::with_capacity(INITIAL_BUFFER_CAPACITY);
                        if config.idle_malloc_trim {
                            // SAFETY: 0 is always a valid argument to `malloc_trim`.
                            #[cfg(target_os = "linux")]
                            unsafe {
                                libc::malloc_trim(0);
                            }
                        }
                    }
                    // Read the message without any timeout.
                    // The loop is not strictly necessary, but we keep it in
                    // order to make the code robust against failures in
                    // updating the socket timeout.
                    loop {
                        if let Some(bytes) = reader.receive_message(&mut buf, &mut fds, 0, None) {
                            break bytes;
                        }
                    }
                }
            };

        if num_bytes_received <= 0 {
            break;
        }
    }
}
```

**File:** clippy.toml (L5-5)
```text
    { path = "bincode::deserialize_from", reason = "bincode::deserialize_from() is not safe to use on untrusted data, since the method will read a u64 length value from the first 8 bytes of the serialized payload and will then attempt to allocate this number of bytes without any validation." },
```
