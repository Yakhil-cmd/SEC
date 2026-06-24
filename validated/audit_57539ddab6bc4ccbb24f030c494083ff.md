Audit Report

## Title
Unbounded `data.reserve(size)` in `FrameDecoder::decode` Allows Compromised Sandbox to Abort Replica Controller — (`rs/canister_sandbox/src/frame_decoder.rs`)

## Summary
`FrameDecoder::decode` reads an attacker-controlled u64 length prefix from the IPC socket and calls `data.reserve(size)` with no upper-bound guard. A compromised sandbox process holds one end of the `UnixStream::pair()` socket and can write 8 crafted bytes to trigger an allocation of `usize::MAX` bytes, causing the Rust allocator to call `handle_alloc_error` → `abort()`, killing the replica controller process.

## Finding Description
In `rs/canister_sandbox/src/frame_decoder.rs`, the `decode` method reads an 8-byte u64 length prefix and immediately casts it to `usize` with no bounds check:

```rust
FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    if data.len() < size {
        data.reserve(size);   // ← no upper-bound guard
        return None;
    }
``` [1](#0-0) 

Sending `0xFFFFFFFFFFFFFFFF` as the 8-byte big-endian prefix causes `size as usize = usize::MAX`. `BytesMut::reserve(usize::MAX)` calls the global allocator with an impossible request; Rust's allocator calls `handle_alloc_error` → `abort()`, terminating the process.

The call chain is confirmed: `socket_read_messages` in `rs/canister_sandbox/src/transport.rs` creates a `FrameDecoder` and calls `decoder.decode(&mut buf)` in a loop: [2](#0-1) 

The `CanisterSandboxIPC` thread in the replica controller calls `socket_read_messages` to read `SandboxToController` messages from each sandbox child process: [3](#0-2) 

The socket is a `UnixStream::pair()` — the sandbox process holds one end and can write arbitrary bytes to it: [4](#0-3) 

The same unbounded `reserve` pattern exists in the `SandboxLauncherIPC` thread path: [5](#0-4) 

A grep for `MAX_FRAME`, `max_frame`, or any frame size constant across the entire `rs/canister_sandbox/` tree returns no results — there is no existing guard anywhere in the IPC stack.

## Impact Explanation
A compromised sandbox process can crash the replica controller process by writing 8 bytes to its legitimate socket file descriptor. This halts the replica node. If the same canister is executed across multiple nodes in a subnet simultaneously, all affected nodes crash, breaking consensus liveness. This matches the allowed High impact: **"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."** Severity: **High ($2,000–$10,000)**.

## Likelihood Explanation
The canister sandbox is explicitly a defense-in-depth security boundary; the design assumes the sandbox process may be compromised (e.g., via a Wasm executor bug exploited by a malicious canister). The IPC socket is the only communication channel, and the controller unconditionally trusts the length prefix. No authentication, HMAC, or size cap exists on incoming frames. Exploiting this requires only writing 8 bytes to a file descriptor the sandbox process legitimately holds — no memory corruption, no heap spray, no timing dependency. The attack is deterministic and repeatable.

## Recommendation
Add a maximum frame size guard before `data.reserve(size)` in `FrameDecoder::decode` at `rs/canister_sandbox/src/frame_decoder.rs` line 45:

```rust
FrameDecoderState::Length(size) => {
    let size: usize = *size as usize;
    const MAX_FRAME_SIZE: usize = 256 * 1024 * 1024; // 256 MiB
    if size > MAX_FRAME_SIZE {
        panic!("frame size {} exceeds maximum allowed {}", size, MAX_FRAME_SIZE);
    }
    if data.len() < size {
        data.reserve(size);
        return None;
    }
```

The constant should be chosen to exceed the largest legitimate IPC message (e.g., Wasm module uploads), and the guard should be placed before any allocation.

## Proof of Concept
```rust
// Unit test / fuzz target: feed crafted bytes into FrameDecoder
use bytes::BytesMut;
use ic_canister_sandbox::frame_decoder::FrameDecoder;

let mut decoder = FrameDecoder::<SomeMessage>::new();
let mut buf = BytesMut::new();
// Write a u64 length of usize::MAX in big-endian (8 bytes)
buf.extend_from_slice(&0xFFFFFFFFFFFFFFFFu64.to_be_bytes());
// This call aborts the process via OOM handler:
decoder.decode(&mut buf);
```

On a real node: from within a compromised sandbox process, write `\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF` to fd 3. The controller's `CanisterSandboxIPC` thread calls `FrameDecoder::decode`, hits `data.reserve(usize::MAX)`, and the replica process aborts immediately.

### Citations

**File:** rs/canister_sandbox/src/frame_decoder.rs (L44-48)
```rust
                FrameDecoderState::Length(size) => {
                    let size: usize = *size as usize;
                    if data.len() < size {
                        data.reserve(size);
                        return None;
```

**File:** rs/canister_sandbox/src/transport.rs (L442-447)
```rust
    let mut decoder = FrameDecoder::<Message>::new();
    let mut buf = BytesMut::with_capacity(INITIAL_BUFFER_CAPACITY);
    let mut fds = Vec::<RawFd>::new();
    let mut reader = SocketReaderWithTimeout::new(socket);
    loop {
        while let Some(mut frame) = decoder.decode(&mut buf) {
```

**File:** rs/canister_sandbox/src/replica_controller/launch_as_process.rs (L47-63)
```rust
    let _ = std::thread::Builder::new()
        .name("SandboxLauncherIPC".to_string())
        .spawn(move || {
            let demux = transport::Demux::<_, _, protocol::transport::LauncherToController>::new(
                Arc::new(rpc::ServerStub::new(
                    controller_service,
                    out.make_sink::<protocol::ctllaunchersvc::Reply>(),
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
