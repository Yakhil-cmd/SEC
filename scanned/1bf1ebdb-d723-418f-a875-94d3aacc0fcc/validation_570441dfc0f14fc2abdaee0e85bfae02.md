The code is clear enough to make a definitive determination. Let me trace the exact logic.

### Title
FD Leakage in Replica IPC Transport via Excess SCM_RIGHTS from Compromised Sandbox — (`rs/canister_sandbox/src/transport.rs`)

### Summary

`install_file_descriptors` fails to close or drain extra file descriptors when a received IPC message carries more FDs in SCM_RIGHTS ancillary data than its `enumerate_fds` slots expect. Because the `fds` accumulator vector is shared across all messages in the `socket_read_messages` loop, these extras persist indefinitely in the replica process without ever being closed, constituting a real FD leak exploitable by a compromised sandbox.

### Finding Description

**Root cause — `install_file_descriptors` (`transport.rs:488-505`)** [1](#0-0) 

When `fd_locs.len() < fds.len()` (more FDs received than the message type declares slots for), the function executes:

```rust
fds.drain(0..fd_locs.len());   // only removes the "consumed" count
// extras at indices [fd_locs.len()..fds.len()] remain — never closed
```

The `fds` vector is allocated once per connection and reused across every message in the loop: [2](#0-1) 

**The receive path blindly accepts all SCM_RIGHTS FDs** with no count validation: [3](#0-2) 

**The sandbox→controller message types carry zero FD slots.** Every variant of `ctlsvc::Request` (`ExecutionFinished`, `ExecutionPaused`, `LogViaReplica`) implements `enumerate_fds` as a no-op: [4](#0-3) 

This means `fd_locs.len() == 0` for every normal sandbox→controller message. When the sandbox attaches N extra FDs to such a message, `fds.drain(0..0)` removes nothing, and all N FDs remain in the vector permanently. They are open kernel file descriptions in the replica process and are never closed.

**The replica's IPC reader for the sandbox channel** is started here: [5](#0-4) 

### Impact Explanation

A compromised sandbox process can send a rapid stream of `ExecutionFinished` (or any zero-FD-slot) messages, each carrying up to `MAX_NUM_FD_PER_MESSAGE = 16` extra FDs in SCM_RIGHTS. [6](#0-5) 

Each `recvmsg` call creates new kernel file descriptions in the replica process (SCM_RIGHTS duplicates the FDs into the receiver). These accumulate without bound. Once the replica's open-FD limit is exhausted, every subsequent `socket()`, `open()`, `spawn_socketed_process()`, or `memfd_create()` call fails with `EMFILE`, breaking canister execution, state sync, and inter-process communication for the entire replica node.

### Likelihood Explanation

**Prerequisite**: the sandbox process must already be compromised (e.g., via a Wasm sandbox escape). This is a non-trivial prerequisite. However, the IC's own security model explicitly treats the sandbox as an untrusted peer — the SELinux policy document states the sandbox may only interact with the replica "through permitted channels" and acknowledges that a sandbox escape enables further exploitation. The IPC transport is one of those permitted channels and must therefore be hardened against a malicious sender. Given that the replica controller reads from the sandbox socket in a dedicated thread with no FD-count validation, a compromised sandbox can trigger this leak with a single burst of messages.

### Recommendation

In `install_file_descriptors`, explicitly close any FDs that exceed the message's declared slot count before draining them:

```rust
if fd_locs.len() < fds.len() {
    // Close the excess FDs to prevent leakage
    for &fd in &fds[fd_locs.len()..] {
        unsafe { libc::close(fd); }
    }
    fds.drain(0..);   // or fds.clear()
} else {
    fds.clear();
}
```

Additionally, add a defensive assertion or log-and-drop policy in `receive_message` that rejects messages carrying more FDs than `MAX_NUM_FD_PER_MESSAGE` for the expected message type.

### Proof of Concept

```rust
// Unit test sketch (no sandbox escape needed — directly exercises the transport bug)
let mut fds: Vec<RawFd> = vec![];
// Simulate receiving 5 FDs from SCM_RIGHTS for a zero-slot message
let (r1, _w1) = pipe(); let (r2, _w2) = pipe(); /* ... */
fds.extend_from_slice(&[r1, r2, r3, r4, r5]);

let mut msg = ctlsvc::Request::ExecutionFinished(...); // 0 enumerate_fds slots
install_file_descriptors(&mut msg, &mut fds);

// Bug: fds is NOT empty; 5 open FDs remain in the replica process
assert!(fds.is_empty(), "FD leak: {} descriptors not closed", fds.len());
// This assertion FAILS with the current code.
```

Repeat this in a loop 200 times (200 × 5 = 1000 FDs) and verify `/proc/self/fd` count grows without bound, eventually causing `open()` to return `EMFILE`.

### Citations

**File:** rs/canister_sandbox/src/transport.rs (L17-18)
```rust
// The maximum number of file descriptors that can be sent in a single message.
const MAX_NUM_FD_PER_MESSAGE: usize = 16;
```

**File:** rs/canister_sandbox/src/transport.rs (L442-450)
```rust
    let mut decoder = FrameDecoder::<Message>::new();
    let mut buf = BytesMut::with_capacity(INITIAL_BUFFER_CAPACITY);
    let mut fds = Vec::<RawFd>::new();
    let mut reader = SocketReaderWithTimeout::new(socket);
    loop {
        while let Some(mut frame) = decoder.decode(&mut buf) {
            install_file_descriptors(&mut frame, &mut fds);
            handler(frame);
        }
```

**File:** rs/canister_sandbox/src/transport.rs (L488-505)
```rust
fn install_file_descriptors<Message: EnumerateInnerFileDescriptors>(
    msg: &mut Message,
    fds: &mut Vec<RawFd>,
) {
    let mut fd_locs = vec![];
    msg.enumerate_fds(&mut fd_locs);
    for n in 0..fd_locs.len() {
        if n < fds.len() {
            *fd_locs[n] = fds[n];
        } else {
            *fd_locs[n] = -1;
        }
    }
    if fd_locs.len() < fds.len() {
        fds.drain(0..fd_locs.len());
    } else {
        fds.clear();
    }
```

**File:** rs/canister_sandbox/src/transport.rs (L662-682)
```rust
            // Push received file descriptors.
            if hdr.msg_controllen > 0 {
                let mut cmsg = libc::CMSG_FIRSTHDR(&hdr);
                while !cmsg.is_null() {
                    if (*cmsg).cmsg_level == libc::SOL_SOCKET
                        && (*cmsg).cmsg_type == libc::SCM_RIGHTS
                    {
                        let data = libc::CMSG_DATA(cmsg);
                        let len = (*cmsg).cmsg_len - libc::CMSG_LEN(0) as MsgControlLenType;
                        let mut pos = 0;
                        while pos + 4 <= len {
                            // Allow `unnecessary_cast` because `len` is `usize`
                            // for linux and `u32` for darwin.
                            #[allow(clippy::unnecessary_cast)]
                            let src = std::slice::from_raw_parts(data.add(pos as usize), 4);
                            let mut raw: [libc::c_uchar; 4] = [0, 0, 0, 0];
                            raw.copy_from_slice(src);
                            let fd = RawFd::from_ne_bytes(raw);
                            fds.push(fd);
                            pos += 4;
                        }
```

**File:** rs/canister_sandbox/src/protocol/ctlsvc.rs (L43-45)
```rust
impl EnumerateInnerFileDescriptors for Request {
    fn enumerate_fds<'a>(&'a mut self, _fds: &mut Vec<&'a mut std::os::unix::io::RawFd>) {}
}
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
