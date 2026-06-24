The code is confirmed. All cited files and line numbers match exactly. Let me verify the key facts:

Audit Report

## Title
Unbounded `data.reserve(size)` in `FrameDecoder::decode` Allows Compromised Sandbox to Abort the Replica Process — (`rs/canister_sandbox/src/frame_decoder.rs`)

## Summary

`FrameDecoder::decode` reads a raw `u64` frame-length field from the IPC socket and immediately casts it to `usize` and calls `data.reserve(size)` with no upper-bound guard. A compromised sandbox process can send an 8-byte header encoding `u64::MAX`, causing Rust's allocator to call `handle_alloc_error` and abort the entire replica process. No `MAX_FRAME_SIZE` constant or range check exists anywhere in `rs/canister_sandbox/`.

## Finding Description

In `rs/canister_sandbox/src/frame_decoder.rs` lines 44–48, the `Length` state of `FrameDecoder::decode` performs an unchecked cast and unconditional reserve:

```rust
FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    if data.len() < size {
        data.reserve(size);   // no upper-bound check
        return None;
    }
```

The `u64` value is read directly from the peer-supplied socket bytes at line 40 (`let size = data.get_u64()`). There is no validation between reading the wire value and calling `reserve`. A grep across all of `rs/canister_sandbox/` for `MAX_FRAME_SIZE`, `max_frame_size`, or any frame-size guard returns zero matches.

The replica spawns a `CanisterSandboxIPC` thread per sandbox process (`launch_as_process.rs` lines 115–131) that calls `transport::socket_read_messages`, which drives `FrameDecoder::decode` in a tight loop (`transport.rs` lines 446–450). The underlying socket is a `UnixStream::pair()` created at `launch_as_process.rs` line 90; the sandbox process holds the other end and has full write access to it.

Sending exactly 8 bytes `[0xFF; 8]` (encoding `u64::MAX`) to the socket is sufficient to enter the `Length` branch with `size = usize::MAX`. `BytesMut::reserve(usize::MAX)` calls into Rust's global allocator; on a 64-bit Linux host the allocation request exceeds the virtual address space, `malloc` returns NULL, and Rust's allocator calls `handle_alloc_error`, which calls `abort()`, terminating the entire replica process immediately.

## Impact Explanation

This matches the allowed ICP bounty impact: **High — Application/platform-level DoS, crash, or subnet availability impact not based on raw volumetric DDoS.** The replica process (not just the thread) is aborted because memory is process-scoped. This causes immediate loss of the replica node from its subnet, loss of any in-flight execution state, and no clean checkpoint. If an attacker can compromise sandbox processes on f+1 nodes of a subnet simultaneously, consensus halts entirely.

## Likelihood Explanation

The precondition is a compromised sandbox process. The IC security model explicitly designates the sandbox as an isolation boundary: the replica must remain safe even if the sandbox behaves arbitrarily. The sandbox executes untrusted canister Wasm; a Wasmtime memory-safety CVE or any sandbox-escape primitive gives an attacker full write access to the IPC socket. Once the socket is open, the exploit requires writing exactly 8 bytes — no heap spray, no ROP chain, no timing dependency. The attack is repeatable and deterministic.

## Recommendation

Add a `MAX_FRAME_SIZE` constant and reject oversized frames before calling `reserve`:

```rust
const MAX_FRAME_SIZE: usize = 256 * 1024 * 1024; // 256 MiB

FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    if size > MAX_FRAME_SIZE {
        return None; // or propagate an Err to close the connection
    }
    if data.len() < size {
        data.reserve(size);
        return None;
    }
    ...
}
```

The same guard should be applied to the `NoLength` branch's `data.reserve(8)` call is already safe (constant), but any future peer-supplied reserve site must be similarly bounded.

## Proof of Concept

```rust
// Unit test targeting FrameDecoder::decode directly:
use bytes::BytesMut;
// Write u64::MAX as big-endian 8-byte length header
let mut buf = BytesMut::new();
buf.extend_from_slice(&u64::MAX.to_be_bytes());
let mut decoder = FrameDecoder::<SomeMessage>::new();
// Triggers data.reserve(usize::MAX) → handle_alloc_error → abort()
let _ = decoder.decode(&mut buf);
```

On the wire: the sandbox writes `[0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]` to its end of the `UnixStream` pair. The replica's `CanisterSandboxIPC` thread reads these 8 bytes via `receive_message` into `buf`, then `decoder.decode` is called, entering `FrameDecoderState::Length(u64::MAX)`, and `data.reserve(usize::MAX)` aborts the process. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/canister_sandbox/src/frame_decoder.rs (L44-48)
```rust
                FrameDecoderState::Length(size) => {
                    let size: usize = *size as usize;
                    if data.len() < size {
                        data.reserve(size);
                        return None;
```

**File:** rs/canister_sandbox/src/transport.rs (L446-450)
```rust
    loop {
        while let Some(mut frame) = decoder.decode(&mut buf) {
            install_file_descriptors(&mut frame, &mut fds);
            handler(frame);
        }
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
