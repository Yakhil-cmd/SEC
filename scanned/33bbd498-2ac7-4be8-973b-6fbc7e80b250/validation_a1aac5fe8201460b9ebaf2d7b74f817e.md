Now I have all the code needed to make a definitive determination. Let me verify the exact controller-side reader thread.

### Title
Unbounded `data.reserve(size)` in `FrameDecoder::decode` Allows Compromised Sandbox to OOM-Crash the Replica Process — (`rs/canister_sandbox/src/frame_decoder.rs`)

---

### Summary

`FrameDecoder::decode` reads a raw `u64` frame-length field from the IPC socket and immediately calls `data.reserve(size)` with no upper-bound guard. A compromised sandbox process can send an 8-byte header encoding `u64::MAX`, causing the replica's `CanisterSandboxIPC` thread to attempt a ~18 EiB allocation, triggering an OOM kill of the entire replica process.

---

### Finding Description

`FrameDecoder::decode` in `rs/canister_sandbox/src/frame_decoder.rs` transitions through two states. In the `Length` state it casts the attacker-controlled `u64` directly to `usize` and calls `data.reserve(size)` with no cap: [1](#0-0) 

There is no `MAX_FRAME_SIZE` constant, no range check, and no error path — the grep search across all of `rs/canister_sandbox/` confirms zero such guards exist.

The replica spawns a dedicated `CanisterSandboxIPC` thread per sandbox process. That thread calls `socket_read_messages`, which drives `FrameDecoder::decode` in a tight loop: [2](#0-1) 

`socket_read_messages` itself also has no frame-size guard before delegating to the decoder: [3](#0-2) 

The socket is a `UnixStream` pair created directly between the replica and the sandbox: [4](#0-3) 

A compromised sandbox process has full write access to its end of this socket. Sending exactly 8 bytes `[0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]` is sufficient to trigger the path.

---

### Impact Explanation

`BytesMut::reserve(usize::MAX)` on a 64-bit Linux host requests ~18 EiB from the allocator. The Linux OOM killer terminates the **entire replica process** (not just the thread), because memory is process-scoped. This causes:

- Immediate loss of the replica node from the subnet.
- Potential consensus gap if enough nodes are targeted simultaneously.
- No clean state checkpoint — the replica dies mid-execution.

---

### Likelihood Explanation

The pre-condition is a compromised sandbox process. The IC security model explicitly treats the sandbox as an isolation boundary: the replica is supposed to remain safe even if the sandbox misbehaves. The sandbox runs untrusted canister Wasm; a Wasm-level sandbox escape (e.g., a Wasmtime CVE) would give an attacker full control of the sandbox process and a direct write path to this socket. The IPC channel itself has no authentication, no message-size limit, and no rate limiting — once the socket is open, a single 8-byte write is sufficient.

---

### Recommendation

Add a `MAX_FRAME_SIZE` constant (e.g., 256 MiB) and reject frames exceeding it before calling `reserve`:

```rust
const MAX_FRAME_SIZE: usize = 256 * 1024 * 1024;

FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    if size > MAX_FRAME_SIZE {
        // close connection / return error
        return None; // or propagate an Err
    }
    if data.len() < size {
        data.reserve(size);
        return None;
    }
    ...
}
```

Apply the same guard in any other location that calls `data.reserve` with a peer-supplied value.

---

### Proof of Concept

```rust
// In a unit test or fuzzer targeting FrameDecoder::decode:
use bytes::BytesMut;
use canister_sandbox::frame_decoder::FrameDecoder;

let mut decoder = FrameDecoder::<SomeMessage>::new();
let mut buf = BytesMut::new();
// Write u64::MAX as big-endian 8-byte length header
buf.extend_from_slice(&u64::MAX.to_be_bytes());
// This call triggers data.reserve(usize::MAX) → OOM
let _ = decoder.decode(&mut buf);
```

On the wire, the sandbox writes 8 bytes to `sock_sandbox`; the replica's `CanisterSandboxIPC` thread reads them via `receive_message` into `buf`, then `decoder.decode` is called, hitting `data.reserve(usize::MAX)`. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/canister_sandbox/src/frame_decoder.rs (L40-48)
```rust
                        let size = data.get_u64();
                        self.state = FrameDecoderState::Length(size);
                    }
                }
                FrameDecoderState::Length(size) => {
                    let size: usize = *size as usize;
                    if data.len() < size {
                        data.reserve(size);
                        return None;
```

**File:** rs/canister_sandbox/src/replica_controller/launch_as_process.rs (L90-99)
```rust
    let (sock_controller, sock_sandbox) = std::os::unix::net::UnixStream::pair()?;
    let request = LaunchSandboxRequest {
        sandbox_exec_path: exec_path.to_string(),
        argv: argv.to_vec(),
        canister_id,
        socket: sock_sandbox.as_raw_fd(),
    };
    let LaunchSandboxReply { pid } = launcher.launch_sandbox(request).sync()?;

    let socket = Arc::new(sock_controller);
```

**File:** rs/canister_sandbox/src/replica_controller/launch_as_process.rs (L115-131)
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
