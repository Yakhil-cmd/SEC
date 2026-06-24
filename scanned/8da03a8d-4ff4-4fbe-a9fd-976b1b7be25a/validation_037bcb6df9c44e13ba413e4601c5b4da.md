### Title
Canister Sandbox Write Permission on Shared Memory Files Enables Replica Crash via `ftruncate`/SIGBUS - (File: `ic-os/components/guestos/selinux/ic-node/ic-node.te`)

---

### Summary

The IC canister sandbox SELinux policy grants the `ic_canister_sandbox_t` domain write access to `ic_canister_mem_t`-labeled files (the shared memory files backing canister heap deltas). This write permission implicitly includes the ability to call `ftruncate` to shrink those files. The replica process concurrently `mmap`s the same files; if the sandbox truncates a file below a page the replica has mapped and then accesses, the replica receives `SIGBUS` and crashes. This is explicitly acknowledged as an unresolved security goal violation in the production policy documentation.

---

### Finding Description

The SELinux policy for the GuestOS canister sandbox grants:

```
allow ic_canister_sandbox_t ic_canister_mem_t : file { map read write getattr };
``` [1](#0-0) 

The `ic_canister_mem_t` label covers the files under `/var/lib/ic/data/ic_state/page_deltas/` that back the heap delta for each canister. [2](#0-1) 

The `write` permission on a file object in SELinux implicitly permits `ftruncate`. The production policy documentation explicitly acknowledges this:

> "Additionally, this allows calling ftruncate on the state files. If replica has these files mmapped concurrently, then any access to a page that has been truncated will result in SIGBUS. **This allows crashing the replica through sandbox.**" [3](#0-2) 

And is listed under "Security goal violations":

> "Calling ftruncate on the memory state files allows reducing file size. If replica has these files mapped concurrently and accesses the affected pages, it will be terminated via SIGBUS. **This interaction cannot presently be prevented by policy**, requires some more investigation and/or other mechanism to be put into place." [4](#0-3) 

The replica maps these files with `MAP_SHARED | PROT_READ | PROT_WRITE` via `MmapPageAllocator`: [5](#0-4) 

The `ftruncate64` call used to grow the file is in the replica side: [6](#0-5) 

The sandbox process is explicitly prohibited from opening files itself, but the SELinux policy does not restrict `ftruncate` on already-open file descriptors passed from the replica:

> "We explicitly do not allow the sandbox to open files because they should only be open by the replica" [7](#0-6) 

Additionally, `ic_orchestrator_t`, `ic_replica_t`, and `ic_http_adapter_t` are declared `permissive`, meaning SELinux enforcement is entirely disabled for the replica process itself — only the sandbox domain is enforced: [8](#0-7) 

This is confirmed by the SELinux documentation:

> "SELinux is currently configured to run in enforcing mode for the sandbox and in permissive mode for the rest of the replica." [9](#0-8) 

---

### Impact Explanation

A compromised sandbox process (e.g., via a wasmtime vulnerability triggered by a malicious canister's Wasm payload) can call `ftruncate` on the shared `ic_canister_mem_t` file descriptor it received from the replica. Because the replica has the same file `mmap`-ed with `MAP_SHARED`, any subsequent access by the replica to a page beyond the new truncation boundary delivers `SIGBUS`, terminating the replica process. This crashes the node, removing it from the subnet. If multiple nodes on a subnet are targeted simultaneously, subnet liveness is at risk.

**Impact: 3/5** — Node-level DoS (replica crash), potential subnet liveness degradation if coordinated across nodes.

---

### Likelihood Explanation

The attack requires a first-stage sandbox escape (e.g., a memory-safety bug in wasmtime or the sandbox IPC layer) to gain arbitrary code execution within the `ic_canister_sandbox_t` process. Once that escape is achieved, the `ftruncate` step is trivially enabled by the existing SELinux policy and requires no further privilege escalation. The entry point is a malicious canister deployed by any unprivileged canister developer. The sandbox escape prerequisite is non-trivial but not theoretical — wasmtime has had CVEs, and the IC's own documentation acknowledges this attack path as unresolved.

**Likelihood: 2/5** — Requires sandbox escape as a prerequisite; the `ftruncate` amplification step is then guaranteed by policy.

---

### Recommendation

1. **Apply `F_SEAL_SHRINK` via `fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK)`** on the `memfd`/tmpfs file descriptors before passing them to the sandbox. This prevents any holder of the fd from reducing the file size, eliminating the `ftruncate` attack vector without requiring architectural changes.
2. **Revoke write access before critical mmap windows**: strip the `write` permission from the fd passed to the sandbox after the initial setup phase, or use separate read-only fds for the replica's mmap.
3. **Pursue the `memfd` sealing approach** already identified in the documentation as a remedy.
4. **Transition `ic_replica_t` out of permissive mode** to enforce the full SELinux policy on the replica process. [10](#0-9) 

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Attacker deploys a malicious canister containing Wasm that exploits a wasmtime memory-safety bug (e.g., an out-of-bounds write in the JIT-compiled code path) to achieve arbitrary code execution within the `ic_canister_sandbox_t` process.

2. The sandbox process holds an open file descriptor to the `ic_canister_mem_t`-labeled file (passed by the replica via `LaunchSandboxRequest`): [11](#0-10) 

3. The attacker's shellcode calls:
   ```c
   ftruncate(fd, 0);  // shrink the shared memory file to zero bytes
   ```
   This is permitted by the SELinux policy (`write` on `ic_canister_mem_t`).

4. The replica, which has the same file mapped via `MAP_SHARED`, accesses a page at offset > 0: [12](#0-11) 

5. The kernel delivers `SIGBUS` to the replica process. The replica crashes. The node goes offline.

The SELinux policy explicitly confirms no rule prevents this:

> "This interaction cannot presently be prevented by policy." [13](#0-12)

### Citations

**File:** ic-os/components/guestos/selinux/ic-node/ic-node.te (L25-27)
```text
permissive ic_orchestrator_t;
permissive ic_replica_t;
permissive ic_http_adapter_t;
```

**File:** ic-os/components/guestos/selinux/ic-node/ic-node.te (L313-317)
```text
# Allow read/write access to files that back the heap delta for both sandbox and replica
# The workflow is that the replica creates the files but passes a file descriptor to the sandbox
# We explicitly do not allow the sandbox to open files because they should only be open by the replica
allow ic_canister_sandbox_t ic_canister_mem_t : file { map read write getattr };
allow ic_replica_t ic_canister_mem_t : file { map read write getattr };
```

**File:** ic-os/components/guestos/selinux/ic-node/ic-node.fc (L8-8)
```text
/var/lib/ic/data/ic_state/page_deltas(/.*)?    gen_context(system_u:object_r:ic_canister_mem_t,s0)
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L261-265)
```text
_Side effects_: Formally it allows sandbox to read/write arbitrary state files set up by replica (even those of other canisters). However,
sandbox cannot actively _open_ any of these files. It can in fact only access files through descriptors that are passed by replica. So
replica is the ultimate arbiter on which files of this type are made accessible to each sandbox process. Additionally, this allows
calling ftruncate on the state files. If replica has these files mmapped concurrently, then any access to a page that has been truncated
will result in SIGBUS. This allows crashing the replica through sandbox.
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L323-328)
```text
===== Security goal violations

Presently, the security policy as implemented allows some interactions that are not as desired:

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

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L606-616)
```rust
        // SAFETY: The parameters are valid.
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

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L828-831)
```rust
#[cfg(target_os = "linux")]
unsafe fn truncate_file(fd: RawFd, offset: FileOffset) -> c_int {
    unsafe { libc::ftruncate64(fd, offset) }
}
```

**File:** ic-os/docs/SELinux.adoc (L3-5)
```text
SELinux is currently configured to run in enforcing mode for the sandbox and in permissive mode for the rest of the replica (Note: Technically, SELinux is running in enforcing mode, but only the sandbox has a written-out policy. Most other domains are marked as "permissive").

This means that the SELinux policy is enforced only for the sandbox, and just used to monitor and log access requests on the rest of the replica.
```

**File:** rs/canister_sandbox/src/replica_controller/launch_as_process.rs (L90-97)
```rust
    let (sock_controller, sock_sandbox) = std::os::unix::net::UnixStream::pair()?;
    let request = LaunchSandboxRequest {
        sandbox_exec_path: exec_path.to_string(),
        argv: argv.to_vec(),
        canister_id,
        socket: sock_sandbox.as_raw_fd(),
    };
    let LaunchSandboxReply { pid } = launcher.launch_sandbox(request).sync()?;
```
