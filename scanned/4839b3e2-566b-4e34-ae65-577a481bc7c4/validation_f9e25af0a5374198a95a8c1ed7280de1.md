### Title
Sandbox Process Can Crash Replica via `ftruncate` on Shared Backing fd — (`rs/replicated_state/src/page_map/page_allocator/mmap.rs`)

### Summary

A malicious canister's sandbox process receives the backing memfd file descriptor via `SCM_RIGHTS`. The SELinux policy grants `write` permission on `ic_canister_mem_t` files to `ic_canister_sandbox_t`, which covers `ftruncate`. Calling `ftruncate` on the shared fd changes the file's actual size, causing the `assert_eq!(file_len, self.file_len)` guard in `MmapBasedPageAllocatorCore::new_allocation_area` to fire and panic the replica process. A separate, more direct path also exists: truncating below the currently-mapped region delivers `SIGBUS` to the replica on the next page access. Both paths are explicitly acknowledged as unresolved in the project's own SELinux policy documentation.

---

### Finding Description

**Step 1 — fd delivery to sandbox**

The replica serializes the page allocator's backing fd into `PageAllocatorSerialization` and sends it to the sandbox process via `SCM_RIGHTS` through the Unix-domain socket transport layer. [1](#0-0) [2](#0-1) 

**Step 2 — SELinux allows `ftruncate`**

The policy grants `ic_canister_sandbox_t` the `write` permission on `ic_canister_mem_t` files. Under Linux SELinux, `ftruncate(2)` on an open fd requires only the `write` file permission — no separate `truncate` class exists. The policy documentation explicitly confirms this interpretation: [3](#0-2) [4](#0-3) 

**Step 3 — The assert in `new_allocation_area`**

Every time the bump-pointer allocation area is exhausted, the replica calls `new_allocation_area`. It reads the actual file length with `get_file_length()` and immediately asserts it equals the internally tracked `self.file_len`. If the sandbox has called `ftruncate` at any point since the last allocation (no race window required — the modification persists), the assert fires: [5](#0-4) 

**Step 4 — Alternative direct path: SIGBUS**

If the sandbox truncates the file to a length shorter than an already-mapped region, the next replica read or write to any page in the truncated range delivers `SIGBUS`, terminating the replica process. The documentation explicitly names this as an unresolved policy violation: [6](#0-5) 

---

### Impact Explanation

A single canister's sandbox process can crash the replica process. Because the replica is the single execution engine for the entire subnet, its crash causes subnet-wide unavailability until the node restarts and catches up. This violates the invariant that a single canister cannot affect other canisters or the subnet.

---

### Likelihood Explanation

- Any canister deployer (fully unprivileged) can trigger this.
- The fd is unconditionally passed to every sandbox process.
- The SELinux policy as deployed allows `write` (and therefore `ftruncate`) on the shared files.
- The assert path requires no timing precision — one `ftruncate` call before the next allocation cycle suffices.
- The SIGBUS path requires only that the file be truncated below a mapped offset, which is trivially achievable.
- The project's own documentation lists this as a known, unmitigated security goal violation with no fix deployed. [7](#0-6) 

---

### Recommendation

1. **Immediate**: Apply `F_ADD_SEALS` with `F_SEAL_SHRINK | F_SEAL_GROW` on the memfd before passing it to the sandbox. This prevents any `ftruncate` call from succeeding on the fd, regardless of SELinux. The documentation already identifies this as a candidate remedy.
2. **Medium-term**: Add a dedicated SELinux file class or use `fsetfilecon` to label the shared memfd with a type that explicitly denies `write` (and therefore `ftruncate`) to `ic_canister_sandbox_t` while still permitting `map`, `read`, and `getattr`.
3. **Long-term**: Restructure so the replica does not keep the backing file mmapped while the sandbox holds a writable fd to it.

---

### Proof of Concept

```rust
// Inside malicious canister native code (sandbox process context):
// The sandbox receives the page allocator fd via SCM_RIGHTS as `backing_fd`.

// Thread 1: continuously ftruncate to a different size
std::thread::spawn(move || {
    loop {
        // Truncate to 0 — any value != current file_len triggers the assert
        unsafe { libc::ftruncate(backing_fd, 0); }
        // Restore to avoid SIGBUS on already-mapped pages (optional)
        unsafe { libc::ftruncate(backing_fd, original_len); }
    }
});

// Result: on the next call to new_allocation_area in the replica,
// get_file_length() returns 0 (or original_len after restore races),
// assert_eq!(file_len, self.file_len) fires → replica panics → subnet down.

// Simpler single-shot variant (no race needed):
// ftruncate once to any size != current file_len, then wait.
// The assert fires on the very next allocation slow-path in the replica.
unsafe { libc::ftruncate(backing_fd, 0); }
```

### Citations

**File:** rs/canister_sandbox/src/protocol/sbxsvc.rs (L141-145)
```rust
impl EnumerateInnerFileDescriptors for PageAllocatorSerialization {
    fn enumerate_fds<'a>(&'a mut self, fds: &mut Vec<&'a mut std::os::unix::io::RawFd>) {
        fds.push(&mut self.fd.fd);
    }
}
```

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L243-248)
```rust
        PageAllocatorSerialization {
            id: core.id,
            fd: FileDescriptor {
                fd: core.file_descriptor,
            },
        }
```

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L583-593)
```rust
    fn new_allocation_area(&mut self) -> AllocationArea {
        let mmap_pages = self.get_amortized_chunk_size_in_pages();
        let mmap_size = mmap_pages * PAGE_SIZE;
        let mmap_file_offset = self.file_len;

        // SAFETY: The file descriptor is valid.
        let file_len = unsafe { get_file_length(self.file_descriptor) };

        // Allocation is the only operation that modifies the file size.
        // Ensure that the file size did not change since the last allocation.
        assert_eq!(file_len, self.file_len);
```

**File:** ic-os/components/guestos/selinux/ic-node/ic-node.te (L313-316)
```text
# Allow read/write access to files that back the heap delta for both sandbox and replica
# The workflow is that the replica creates the files but passes a file descriptor to the sandbox
# We explicitly do not allow the sandbox to open files because they should only be open by the replica
allow ic_canister_sandbox_t ic_canister_mem_t : file { map read write getattr };
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L263-265)
```text
replica is the ultimate arbiter on which files of this type are made accessible to each sandbox process. Additionally, this allows
calling ftruncate on the state files. If replica has these files mmapped concurrently, then any access to a page that has been truncated
will result in SIGBUS. This allows crashing the replica through sandbox.
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L323-336)
```text
===== Security goal violations

Presently, the security policy as implemented allows some interactions that are not as desired:

Allowing getsched on ic_canister_sandbox_t domain may allow to learn about “existence” of other sandbox processes (by probing pid space). No other information can be obtained through this mechanism. While this does not appear to be harmful, it should be investigated whether the underlying interaction can simply also be denied.
Calling +ftruncate+ on the memory state files allows reducing file size. If replica has these files mapped concurrently and accesses the affected pages, it will by terminated via SIGBUS. This interaction cannot presently be prevented by policy, requires some more investigation and/or other mechanism to be put into place (e.g. not use the files in memory-mapped inside replica).

*Remedies for the ftruncate problem*

* different software architecture that does not require replica to mmap anymore
** in principle it would not need to mmap, it only needs to deal with memory contents at checkpoint time. It might as well read because the data is processed only once
* various ways to revoke “write” access before critical points in time
* adding capability to deny “ftruncate”
* use memfd truncate sealing support, but that also requires some architecture changes because expanding memory area requires sandbox/replica ipc
```
