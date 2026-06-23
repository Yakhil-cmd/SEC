Based on the full code in `rs/canister_sandbox/src/transport.rs`, here is my analysis:

---

### Title
Unbounded FD Accumulation in `socket_read_messages` Allows Compromised Sandbox to Exhaust Replica FD Table — (`rs/canister_sandbox/src/transport.rs`)

### Summary

`socket_read_messages` accumulates received `SCM_RIGHTS` file descriptors into a `fds: Vec<RawFd>` without any bound or cleanup when frames are incomplete. A compromised sandbox process can send an unbounded stream of partial frames each carrying ancillary FDs, exhausting the replica controller's open-file-descriptor table.

### Finding Description

In `socket_read_messages` (lines 434–484), the loop structure is:

1. Try to decode a complete frame from `buf`; if one is ready, call `install_file_descriptors` and dispatch it.
2. Call `receive_message`, which calls `recvmsg` and **appends every received SCM_RIGHTS FD to `fds`** (line 680: `fds.push(fd)`).
3. If no complete frame is decoded, go back to step 2 — `fds` is never drained. [1](#0-0) 

`install_file_descriptors` is only called when `decoder.decode` returns `Some(frame)`. Until that happens, every `recvmsg` call that carries ancillary data appends raw FDs to `fds` with no upper-bound check and no `close()` call on excess entries. [2](#0-1) 

The sender-side constant `MAX_NUM_FD_PER_MESSAGE = 16` is enforced only on the **sending** path. [3](#0-2) 

The receiver's `CONTROL_MESSAGE_SIZE = 4096` bytes can hold up to ~1024 FDs per `recvmsg` call, and the receiver imposes no cap on how many it accepts or accumulates. [4](#0-3) 

### Impact Explanation

Each FD received via `SCM_RIGHTS` is a real, open file descriptor in the **replica (controller) process**. Accumulating thousands of them exhausts the per-process FD limit (default soft limit 1024 on Linux). Once exhausted, the replica cannot open new sockets, files, or pipes — breaking consensus participation, P2P networking, and state sync. The replica process effectively becomes non-functional without crashing, making recovery harder to detect.

### Likelihood Explanation

The precondition is a compromised sandbox process. IC's security model is explicitly defense-in-depth: the sandbox is isolated via seccomp/namespaces precisely because canister Wasm execution is untrusted. A canister that exploits a memory-safety bug in the Wasm runtime or sandbox binary gains code execution in the sandbox process and can immediately trigger this path. The exploit itself is trivial once the sandbox is controlled: send a stream of 1-byte partial frames each with 16 SCM_RIGHTS FDs pointing to `/dev/null` duplicates. No complete frame is ever assembled; `fds` grows without bound.

### Recommendation

1. **Enforce a hard cap on `fds.len()`** inside the `socket_read_messages` loop. If `fds.len()` exceeds a reasonable maximum (e.g., `MAX_NUM_FD_PER_MESSAGE * 2`), close all excess FDs and terminate the connection.
2. **Close unconsumed FDs** in `install_file_descriptors` when `fd_locs.len() < fds.len()` — the current code drains the used slots but leaves the rest open for the next message. Any FDs beyond what the next decoded message needs should be explicitly closed.
3. Consider adding a **maximum accumulated-bytes limit** on `buf` as well, to prevent a parallel memory-exhaustion attack via partial frames.

### Proof of Concept

```
// Pseudocode for compromised sandbox process
loop {
    // Send 1 byte of payload (partial frame, never completes a frame)
    // with 16 SCM_RIGHTS FDs (e.g., dups of /dev/null)
    sendmsg(controller_socket,
        iov=[b'\x00'],
        cmsg=SCM_RIGHTS([dup("/dev/null")] * 16));
}
// After ~64 iterations the replica's FD table (soft limit 1024) is exhausted.
// Replica can no longer accept new connections or open files.
``` [5](#0-4)

### Citations

**File:** rs/canister_sandbox/src/transport.rs (L17-18)
```rust
// The maximum number of file descriptors that can be sent in a single message.
const MAX_NUM_FD_PER_MESSAGE: usize = 16;
```

**File:** rs/canister_sandbox/src/transport.rs (L21-21)
```rust
const CONTROL_MESSAGE_SIZE: usize = 4096;
```

**File:** rs/canister_sandbox/src/transport.rs (L442-484)
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

**File:** rs/canister_sandbox/src/transport.rs (L488-506)
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
}
```

**File:** rs/canister_sandbox/src/transport.rs (L658-686)
```rust
        if num_bytes_received > 0 {
            // Update the buffer length to account for the received bytes.
            buf.set_len(buf.len() + (num_bytes_received as usize));

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
                    }
                    cmsg = libc::CMSG_NXTHDR(&hdr, cmsg);
                }
            }
```
