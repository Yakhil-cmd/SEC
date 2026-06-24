### Title
Sandbox Process Can Crash Replica via `ftruncate()` on Shared `MAP_SHARED` Heap-Delta File — (`ic-os/guestos/docs/SELinux-Policy.adoc`, `rs/replicated_state/src/page_map/page_allocator/mmap.rs`)

---

### Summary

A compromised canister sandbox process can call `ftruncate(fd, 0)` on a heap-delta file descriptor that was passed to it by the replica. Because the replica maps the same file with `MAP_SHARED`, any subsequent replica access to a page beyond the new file size delivers `SIGBUS` to the replica process, crashing it. The DFINITY SELinux policy documentation explicitly acknowledges this as an unmitigated security goal violation.

---

### Finding Description

**Three independent facts combine to make this exploitable:**

**1. The SELinux policy grants `write` on `ic_canister_mem_t` files to the sandbox domain.**

The policy in `ic-os/components/guestos/selinux/ic-node/ic-node.te` grants:

```
allow ic_canister_sandbox_t ic_canister_mem_t : file { map read write getattr };
``` [1](#0-0) 

On Linux, `write` permission on a file descriptor is sufficient to call `ftruncate()` on it. There is no separate SELinux `truncate` permission that is denied.

**2. The replica maps heap-delta files with `MAP_SHARED`.**

In `rs/replicated_state/src/page_map/page_allocator/mmap.rs`, the `MmapBasedPageAllocatorCore::new_allocation_area()` function maps the backing file with `MapFlags::MAP_SHARED`:

```rust
mmap(
    std::ptr::null_mut(),
    mmap_size,
    ProtFlags::PROT_READ | ProtFlags::PROT_WRITE,
    MapFlags::MAP_SHARED,
    self.file_descriptor,
    mmap_file_offset,
)
``` [2](#0-1) 

The same `MAP_SHARED` flag is used in `grow_for_deserialization()`: [3](#0-2) 

With `MAP_SHARED`, if the underlying file is truncated, any access to a page beyond the new file size delivers `SIGBUS` to the accessing process — in this case, the replica.

**3. The DFINITY documentation explicitly acknowledges this as an unmitigated security goal violation.**

From `ic-os/guestos/docs/SELinux-Policy.adoc`:

> "Additionally, this allows calling ftruncate on the state files. If replica has these files mmapped concurrently, then any access to a page that has been truncated will result in SIGBUS. **This allows crashing the replica through sandbox.**" [4](#0-3) 

> "Calling ftruncate on the memory state files allows reducing file size. If replica has these files mapped concurrently and accesses the affected pages, it will by terminated via SIGBUS. **This interaction cannot presently be prevented by policy**, requires some more investigation and/or other mechanism to be put into place." [5](#0-4) 

The file descriptor flow is: replica creates the backing file via `memfd_create` or a `.mem` file, maps it `MAP_SHARED`, then passes the fd to the sandbox via the Unix socket IPC channel (`socket_read_messages` / `send_message` in `rs/canister_sandbox/src/transport.rs`). [6](#0-5) 

The sandbox receives a valid, writable fd for `ic_canister_mem_t`-labeled files. A compromised sandbox can immediately call `ftruncate(fd, 0)` without any further privilege escalation.

---

### Impact Explanation

- A compromised sandbox process calls `ftruncate(received_fd, 0)`.
- The replica's next access to any page in its `MAP_SHARED` mapping of that file receives `SIGBUS`.
- `SIGBUS` is not caught by the replica's signal handlers for this mapping; the replica process terminates.
- **Scoped impact**: single replica node crash. The subnet continues with the remaining replicas but loses one node's participation, degrading fault tolerance. Repeated exploitation across multiple sandboxes could reduce subnet availability below the fault threshold.

---

### Likelihood Explanation

The precondition is a compromised sandbox process. This requires a prior Wasm sandbox escape (e.g., a memory-safety bug in the Wasm execution engine or embedder). While sandbox escapes are non-trivial, they are a known attack surface for canister execution environments. Once a sandbox escape is achieved, the `ftruncate` path to crash the replica is **trivial, deterministic, and requires no additional privileges** — the fd is already in the sandbox's possession. The DFINITY team has confirmed the mechanism works and has not yet deployed a fix.

---

### Recommendation

The DFINITY documentation lists the following remedies: [7](#0-6) 

The most immediately deployable mitigations are:
- **`memfd` sealing**: Call `fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK)` on the backing `memfd` before passing it to the sandbox. This prevents `ftruncate` from reducing the file size. The replica would need to use a separate grow-only protocol for expanding the file.
- **`SIGBUS` handler**: Install a `SIGBUS` signal handler in the replica that catches accesses to truncated pages and terminates gracefully rather than crashing, preserving subnet participation.
- **`MAP_PRIVATE` for replica-side reads**: Where the replica only needs to read page delta contents (e.g., at checkpoint time), use `MAP_PRIVATE` instead of `MAP_SHARED`. A truncation of the underlying file does not deliver `SIGBUS` to `MAP_PRIVATE` mappings of already-faulted pages.

---

### Proof of Concept

```c
// Parent: replica analog
int fd = memfd_create("heap_delta", 0);
ftruncate(fd, 4096);
void *map = mmap(NULL, 4096, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
memset(map, 0xAA, 4096);  // touch the page

// Pass fd to child via SCM_RIGHTS (simulating sandbox fd handoff)
// ... sendmsg with SCM_RIGHTS ...

// Child: sandbox analog — calls ftruncate to zero the file
ftruncate(received_fd, 0);

// Parent: replica accesses the now-truncated mapping
char val = ((char*)map)[0];  // SIGBUS delivered here → replica crashes
```

This is directly testable locally with a `memfd_create` + `fork` + `SCM_RIGHTS` harness, matching the production IPC path in `rs/canister_sandbox/src/transport.rs`.

### Citations

**File:** ic-os/components/guestos/selinux/ic-node/ic-node.te (L313-317)
```text
# Allow read/write access to files that back the heap delta for both sandbox and replica
# The workflow is that the replica creates the files but passes a file descriptor to the sandbox
# We explicitly do not allow the sandbox to open files because they should only be open by the replica
allow ic_canister_sandbox_t ic_canister_mem_t : file { map read write getattr };
allow ic_replica_t ic_canister_mem_t : file { map read write getattr };
```

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L607-616)
```rust
        let mmap_ptr = unsafe {
            mmap(
                std::ptr::null_mut(),
                mmap_size,
                ProtFlags::PROT_READ | ProtFlags::PROT_WRITE,
                MapFlags::MAP_SHARED,
                self.file_descriptor,
                mmap_file_offset,
            )
        }
```

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L692-701)
```rust
        let mmap_ptr = unsafe {
            mmap(
                std::ptr::null_mut(),
                mmap_size,
                ProtFlags::PROT_READ | ProtFlags::PROT_WRITE,
                MapFlags::MAP_SHARED,
                self.file_descriptor,
                mmap_file_offset,
            )
        }
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L263-265)
```text
replica is the ultimate arbiter on which files of this type are made accessible to each sandbox process. Additionally, this allows
calling ftruncate on the state files. If replica has these files mmapped concurrently, then any access to a page that has been truncated
will result in SIGBUS. This allows crashing the replica through sandbox.
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L327-328)
```text
Allowing getsched on ic_canister_sandbox_t domain may allow to learn about “existence” of other sandbox processes (by probing pid space). No other information can be obtained through this mechanism. While this does not appear to be harmful, it should be investigated whether the underlying interaction can simply also be denied.
Calling +ftruncate+ on the memory state files allows reducing file size. If replica has these files mapped concurrently and accesses the affected pages, it will by terminated via SIGBUS. This interaction cannot presently be prevented by policy, requires some more investigation and/or other mechanism to be put into place (e.g. not use the files in memory-mapped inside replica).
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L330-336)
```text
*Remedies for the ftruncate problem*

* different software architecture that does not require replica to mmap anymore
** in principle it would not need to mmap, it only needs to deal with memory contents at checkpoint time. It might as well read because the data is processed only once
* various ways to revoke “write” access before critical points in time
* adding capability to deny “ftruncate”
* use memfd truncate sealing support, but that also requires some architecture changes because expanding memory area requires sandbox/replica ipc
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
