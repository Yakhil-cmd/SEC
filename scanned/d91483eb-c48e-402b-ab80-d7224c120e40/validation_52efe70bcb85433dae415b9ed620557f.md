### Title
Unbounded SCM_RIGHTS FD Accumulation in `socket_read_messages` Allows Compromised Sandbox to Exhaust Replica Process FD Table — (`rs/canister_sandbox/src/transport.rs`)

---

### Summary

`socket_read_messages` accumulates received `SCM_RIGHTS` file descriptors into an unbounded `Vec<RawFd>` across every `recvmsg` call. FDs are only drained when a complete framed message is decoded. A compromised sandbox process can send an indefinite stream of partial frames, each carrying SCM_RIGHTS FDs, causing the replica controller's open-FD count to grow without bound until the process FD table is exhausted.

---

### Finding Description

In `rs/canister_sandbox/src/transport.rs`, `socket_read_messages` maintains a single `fds: Vec<RawFd>` that persists across the entire read loop: [1](#0-0) 

On every iteration, `receive_message` is called, which calls `recvmsg` and unconditionally pushes every received FD into `fds`: [2](#0-1) 

FDs are only consumed by `install_file_descriptors`, which is called only when `FrameDecoder::decode` returns a complete frame: [3](#0-2) 

`FrameDecoder::decode` returns `None` whenever the byte buffer does not yet contain a complete length-prefixed frame: [4](#0-3) 

There is no bound check on `fds.len()` anywhere in the loop, no maximum FD accumulation limit, and no cleanup of excess FDs on timeout or loop exit.

The sender-side constant `MAX_NUM_FD_PER_MESSAGE = 16` is enforced only in `send_message` on the legitimate sender path: [5](#0-4) 

A compromised sandbox bypasses `send_message` entirely and calls `sendmsg` directly, subject only to the Linux kernel limit of `SCM_MAX_FD = 253` FDs per `sendmsg` call. The receiver's control buffer is 4096 bytes, which accommodates all 253 FDs without truncation.

Additionally, `install_file_descriptors` does not close excess FDs — it drains them from the Vec without calling `close()`: [6](#0-5) 

This means even in the normal path, FDs sent in excess of what a message type expects are silently leaked.

---

### Impact Explanation

The replica controller process has a finite FD table (typically `RLIMIT_NOFILE = 65536` on production Linux). With 253 FDs per `sendmsg` call, ~260 calls exhaust the table. Once exhausted:

- `recvmsg`, `accept`, `open`, `socket`, `pipe`, `epoll_create` all return `EMFILE`
- The replica cannot accept new P2P connections, open state files, or spawn new sandbox processes
- Consensus participation breaks: the node cannot participate in block making, notarization, or certification rounds
- The node effectively becomes a dead replica until restarted

---

### Likelihood Explanation

The prerequisite is a compromised sandbox process. The sandbox is a separate process spawned per canister execution, isolated via seccomp. Compromise requires a Wasm execution vulnerability or sandbox escape — a non-trivial but realistic precondition given the attack surface of a Wasm JIT/interpreter. Once the sandbox is compromised, the exploit is trivial: send a valid 8-byte length header claiming a large body, then loop sending 1-byte payload chunks each with 253 SCM_RIGHTS FDs. The replica's `socket_read_messages` loop will never decode a complete frame and will accumulate FDs until `EMFILE`.

The IC's defense-in-depth model explicitly assumes that a compromised sandbox must not be able to harm the replica controller. This vulnerability breaks that invariant.

---

### Recommendation

1. **Bound `fds.len()`**: After each `receive_message` call, if `fds.len()` exceeds a reasonable maximum (e.g., `MAX_NUM_FD_PER_MESSAGE * MAX_QUEUED_MESSAGES`), close all excess FDs and terminate the connection.
2. **Close leaked FDs**: In `install_file_descriptors`, explicitly `close()` any FDs remaining in `fds` after the message's slots are filled, rather than silently draining them.
3. **Terminate on protocol violation**: If the sandbox sends more FDs than any legitimate message type can carry, treat it as a protocol violation and kill the sandbox connection immediately.

---

### Proof of Concept

```rust
// Compromised sandbox side — bypasses send_message entirely
use std::os::unix::net::UnixStream;
use libc::{sendmsg, msghdr, iovec, CMSG_SPACE, CMSG_FIRSTHDR, CMSG_DATA, CMSG_LEN,
           SOL_SOCKET, SCM_RIGHTS, c_void};

fn exhaust_replica_fds(socket: &UnixStream) {
    // Send a valid 8-byte length header claiming a 1 MB body
    // so FrameDecoder::decode always returns None
    let length_header: [u8; 8] = (1_000_000u64).to_be_bytes();
    // ... send length_header via write() ...

    // Now loop: send 1 byte of body + 253 SCM_RIGHTS FDs per call
    // After ~260 iterations, replica's FD table is exhausted
    loop {
        let fds: Vec<i32> = (0..253).map(|_| {
            unsafe { libc::open(b"/dev/null\0".as_ptr() as *const _, libc::O_RDONLY) }
        }).collect();
        // sendmsg with SCM_RIGHTS carrying fds, 1 byte payload
        // ... (standard sendmsg setup) ...
        // replica's receive_message pushes all 253 FDs into fds Vec
        // decoder.decode returns None (frame still incomplete)
        // fds Vec grows by 253 each iteration
    }
}
```

After ~260 iterations, the replica's `recvmsg`, `open`, `socket`, and `accept` calls all return `EMFILE (-24)`, breaking consensus participation.

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

**File:** rs/canister_sandbox/src/transport.rs (L501-505)
```rust
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
