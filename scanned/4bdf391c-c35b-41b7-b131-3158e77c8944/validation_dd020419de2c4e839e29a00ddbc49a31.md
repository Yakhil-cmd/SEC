The key code path is fully traceable. Here is the analysis:

**Trace:**

1. `BufferedStableMemReader::new(buffer_size_bytes)` unconditionally calls `reader.read()` at construction. [1](#0-0) 

2. `read()` calls `self.stable_mem.length()` with no guard on page count. [2](#0-1) 

3. `StableMemoryImplementation::length()` delegates directly to `stable::length()`. [3](#0-2) 

4. `stable::length()` unconditionally issues `ic0::stable_read` at offset 0 for 4 bytes — with **no check** that any pages are allocated. [4](#0-3) 

**Contrast with correct guards elsewhere:**

`stable::write()` explicitly skips `length()` when `current_pages == 0`: [5](#0-4) 

`StableReader::default()` also guards with a `stable_size()` check before calling `length()`: [6](#0-5) 

`BufferedStableMemReader::new` has neither guard.

---

### Title
Unconditional `ic0::stable_read` in `BufferedStableMemReader::new` traps when stable memory is empty — (`rs/nervous_system/common/src/dfn_core_stable_mem_utils.rs`)

### Summary
`BufferedStableMemReader::new` calls `stable::length()` unconditionally during construction. `stable::length()` issues `ic0::stable_read(ptr, 0, 4)` regardless of whether any stable memory pages are allocated. When stable memory has 0 pages (e.g., first-ever upgrade of a canister with no `pre_upgrade`), this system call traps, causing `post_upgrade` to fail and permanently bricking the canister.

### Finding Description
`BufferedStableMemReader::new` immediately calls `self.read()`, which calls `self.stable_mem.length()` → `stable::length()`. That function issues `ic0::stable_read` at byte offset 0 for 4 bytes to retrieve the stored length prefix. The IC system call interface traps if the read range falls outside allocated stable memory pages. With 0 pages allocated, any `ic0::stable_read` — even for 4 bytes at offset 0 — is out of bounds and traps.

The sibling `StableReader` in `dfn_core/src/stable.rs` correctly handles this by checking `ic0::stable_size() == 0` before calling `length()`. `BufferedStableMemReader` omits this check entirely.

### Impact Explanation
Any canister that:
- Uses `BufferedStableMemReader::new(...)` in its `post_upgrade` hook, **and**
- Has never run a `pre_upgrade` that wrote to stable memory (i.e., first-ever upgrade, or upgrade after stable memory was never initialized)

...will have its `post_upgrade` trap. A trapping `post_upgrade` causes the upgrade to be rolled back and the canister to remain on the old Wasm — but if the old Wasm also has no `pre_upgrade`, the canister is stuck and cannot be upgraded. This bricks the canister.

NNS/SNS canisters (governance, GTC, etc.) use this reader in their `post_upgrade` hooks.

### Likelihood Explanation
Concrete and locally testable: deploy a canister with no `pre_upgrade` and a `post_upgrade` that calls `BufferedStableMemReader::new(1024)`. Upgrade it. The upgrade will trap. The scenario is realistic for any first-time upgrade of a canister that relies on this reader.

### Recommendation
Add a `stable_size()` guard in `BufferedStableMemReader::new` (or in `read()`) before calling `stable_mem.length()`, mirroring the pattern already used in `StableReader::default()` and `stable::write()`:

```rust
fn read(&mut self) {
    self.buffer.clear();
    // Guard: if no pages are allocated, length() would trap
    if unsafe { ic0::stable_size() } == 0 {
        self.buffer_offset = 0;
        return;
    }
    let stable_mem_len = self.stable_mem.length();
    // ...
}
```

### Proof of Concept
1. Deploy a canister with no `pre_upgrade` hook and a `post_upgrade` that calls `BufferedStableMemReader::new(1024)`.
2. Upgrade the canister.
3. Observe: `post_upgrade` traps with a stable memory out-of-bounds error; the upgrade fails.

### Citations

**File:** rs/nervous_system/common/src/dfn_core_stable_mem_utils.rs (L40-42)
```rust
    fn length(&self) -> u32 {
        stable::length()
    }
```

**File:** rs/nervous_system/common/src/dfn_core_stable_mem_utils.rs (L192-202)
```rust
    pub fn new(buffer_size_bytes: u32) -> Self {
        assert!(buffer_size_bytes > 0, "Buffer size must be greater than 0");
        let mut reader = Self {
            buffer: Vec::with_capacity(buffer_size_bytes as usize),
            buffer_offset: 0,
            stable_mem_offset: 0,
            stable_mem: Box::new(StableMemoryImplementation),
        };
        reader.read();
        reader
    }
```

**File:** rs/nervous_system/common/src/dfn_core_stable_mem_utils.rs (L219-227)
```rust
    fn read(&mut self) {
        self.buffer.clear();
        let stable_mem_len = self.stable_mem.length();
        // Number of bytes to read: minimum of buffer size and remaining amount
        let n_bytes = min(
            self.buffer.capacity() as u32, // cast works as the initialization argument is u32
            stable_mem_len - self.stable_mem_offset,
        );
        self.buffer = self.stable_mem.read(self.stable_mem_offset, n_bytes);
```

**File:** rs/rust_canisters/dfn_core/src/stable.rs (L51-53)
```rust
    let current_pages = unsafe { ic0::stable_size() };

    let old_len = if current_pages == 0 { 0 } else { length() };
```

**File:** rs/rust_canisters/dfn_core/src/stable.rs (L96-102)
```rust
pub fn length() -> u32 {
    let mut len_bytes: [u8; 4] = [0; 4];
    unsafe {
        ic0::stable_read(len_bytes.as_mut_ptr() as u32, 0, LENGTH_BYTES);
    }
    u32::from_le_bytes(len_bytes)
}
```

**File:** rs/rust_canisters/dfn_core/src/stable.rs (L208-216)
```rust
        let num_pages = unsafe { ic0::stable_size() };
        if num_pages == 0 {
            return Self {
                offset: 0,
                bytes_left: 0,
            };
        }

        let bytes_left = length();
```
