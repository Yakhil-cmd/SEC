### Title
Off-by-One in `SliceVec::resize` Boundary Check Causes EVM Heap to Silently Fail at Full Backing-Buffer Capacity - (`File: zk_ee/src/memory/slice_vec.rs`)

---

### Summary

`SliceVec::resize` uses a strict `>=` guard instead of `>`, making the last slot of every backing buffer permanently inaccessible. When `resize_heap_implementation` attempts to grow the EVM heap to exactly the backing-buffer size, the call returns `Err(())`, which is unconditionally mapped to `OutOfGas`. A transaction that legitimately needs that final word of memory is silently killed with an incorrect gas-exhaustion error.

---

### Finding Description

In `zk_ee/src/memory/slice_vec.rs`, the `resize` method reads:

```rust
pub fn resize(&mut self, new_length: usize, padding: T) -> Result<(), ()> {
    if new_length >= self.memory.len() {   // ← off-by-one: should be >
        return Err(());
    }
    …
}
``` [1](#0-0) 

The backing slice `self.memory` has `self.memory.len()` valid `MaybeUninit<T>` slots (indices `0 … len-1`). A `new_length` equal to `self.memory.len()` is a perfectly valid request — it fills the slice exactly — and the subsequent `Deref` / `DerefMut` implementations use `get_unchecked(..self.length)`, which is safe for any `length ≤ memory.len()`. [2](#0-1) 

Because of the `>=` guard, the maximum reachable length is `memory.len() - 1`, so the last slot is always wasted.

The EVM heap is a `SliceVec<'a, u8>` grown through `resize_heap_implementation`:

```rust
heap.resize(new_heap_size, 0)
    .map_err(|_| ExitCode::EvmError(EvmError::OutOfGas))?;
``` [3](#0-2) 

When `new_heap_size` equals the backing-buffer length, `resize` returns `Err(())` and the error is silently promoted to `OutOfGas`. The caller (e.g. `mload`, `mstore`, `calldatacopy`, `sha3`, `log`, etc.) propagates this as a transaction-level gas-exhaustion failure even though the contract has sufficient gas. [4](#0-3) 

---

### Impact Explanation

Any EVM transaction whose memory expansion lands exactly on the backing-buffer boundary receives `OutOfGas` instead of succeeding. This is an EVM semantic mismatch: the standard EVM allows memory to grow to any size bounded only by gas cost, but ZKsync OS silently rejects the last 32-byte word of the allocated region. Concretely:

- A contract that uses exactly `N` bytes of memory (where `N` is the backing-buffer size) will always revert with `OutOfGas`, even with unlimited gas.
- The user loses the gas fee; state changes are rolled back.
- Because the error is indistinguishable from a genuine out-of-gas condition, the root cause is invisible to callers and tooling.

---

### Likelihood Explanation

The backing buffer for the EVM heap is carved from `RunnerMemoryBuffers`. If the buffer is sized to exactly accommodate the EVM memory ceiling enforced by `resize_heap_implementation` (`(u32::MAX - 31).next_multiple_of(32)` bytes), every contract that pushes memory to the limit will hit the bug. Even if the buffer is larger, any contract whose memory footprint happens to equal the buffer size triggers the failure. The entry path is fully unprivileged: any EVM `MLOAD`, `MSTORE`, `CALLDATACOPY`, `SHA3`, `LOG*`, or similar opcode that triggers a heap expansion can reach this code path.

---

### Recommendation

Change the boundary check in `SliceVec::resize` from `>=` to `>`:

```rust
// Before (off-by-one):
if new_length >= self.memory.len() {
    return Err(());
}

// After (correct):
if new_length > self.memory.len() {
    return Err(());
}
``` [1](#0-0) 

This allows the `SliceVec` to be filled to its full capacity, matching the documented semantics and eliminating the spurious `OutOfGas` path.

---

### Proof of Concept

1. Determine the backing-buffer length `N` allocated for the EVM heap in `RunnerMemoryBuffers`.
2. Deploy a contract that executes `MSTORE` at offset `N - 32` (triggering a heap resize to exactly `N` bytes via `next_multiple_of(32)`).
3. Call the contract with sufficient gas.
4. Observe that the transaction reverts with `OutOfGas` despite having enough gas, because `SliceVec::resize(N, 0)` returns `Err(())` and `resize_heap_implementation` maps it to `EvmError::OutOfGas`.

The same failure is reachable through any memory-touching opcode (`MLOAD`, `CALLDATACOPY`, `SHA3`, `LOG*`, `CODECOPY`, `RETURNDATACOPY`, `MCOPY`, `EXTCODECOPY`) whose computed `new_heap_size` equals the backing-buffer length. [5](#0-4) [6](#0-5)

### Citations

**File:** zk_ee/src/memory/slice_vec.rs (L77-80)
```rust
    pub fn resize(&mut self, new_length: usize, padding: T) -> Result<(), ()> {
        if new_length >= self.memory.len() {
            return Err(());
        }
```

**File:** zk_ee/src/memory/slice_vec.rs (L102-119)
```rust
impl<T> Deref for SliceVec<'_, T> {
    type Target = [T];
    fn deref(&self) -> &[T] {
        unsafe {
            let initialized_part = self.memory.get_unchecked(..self.length);
            &*(initialized_part as *const [MaybeUninit<T>] as *const [T])
        }
    }
}

impl<T> DerefMut for SliceVec<'_, T> {
    fn deref_mut(&mut self) -> &mut [T] {
        unsafe {
            let initialized_part = self.memory.get_unchecked_mut(..self.length);
            &mut *(initialized_part as *mut [MaybeUninit<T>] as *mut [T])
        }
    }
}
```

**File:** evm_interpreter/src/utils.rs (L72-93)
```rust
    pub(crate) fn resize_heap_implementation<'a>(
        heap: &mut SliceVec<'a, u8>,
        gas: &mut Gas<S>,
        offset: usize,
        len: usize,
    ) -> Result<(), ExitCode> {
        let max_offset = offset.saturating_add(len);
        let new_heap_size = if max_offset > ((u32::MAX - 31) as usize) {
            return Err(ExitCode::EvmError(EvmError::MemoryLimitOOG));
        } else {
            max_offset.next_multiple_of(32)
        };
        let current_heap_size = heap.len();
        if new_heap_size > current_heap_size {
            gas.pay_for_memory_growth(current_heap_size, new_heap_size)?;

            heap.resize(new_heap_size, 0)
                .map_err(|_| ExitCode::EvmError(EvmError::OutOfGas))?;
        }

        Ok(())
    }
```

**File:** evm_interpreter/src/instructions/heap.rs (L8-31)
```rust
impl<S: EthereumLikeTypes> Interpreter<'_, S> {
    pub fn mload(&mut self, system: &mut System<S>) -> InstructionResult {
        self.gas
            .spend_gas_and_native(gas_constants::VERYLOW, MLOAD_NATIVE_COST)?;
        let stack_top = self.stack.top_mut()?;
        let index = Self::cast_to_usize(stack_top, EvmError::InvalidOperandOOG.into())?;
        Self::resize_heap_implementation(&mut self.heap, &mut self.gas, index, 32)?;
        let mut value: ruint::Uint<256, 4> = U256::ZERO;
        unsafe {
            let src = self.heap.deref_mut().as_ptr().add(index);
            let dst = value.as_le_slice_mut().as_mut_ptr();
            core::ptr::copy_nonoverlapping(src, dst, 32);
            crate::utils::bytereverse_u256(&mut value);
        }

        if Self::PRINT_OPCODES {
            use core::fmt::Write;
            use zk_ee::system_log;
            system_log!(system, " offset: {index}, read value: 0x{value:0x}");
        }

        *stack_top = value;
        Ok(())
    }
```

**File:** evm_interpreter/src/instructions/system.rs (L18-57)
```rust
    pub fn sha3(&mut self, system: &mut System<S>) -> InstructionResult {
        let (memory_offset, len) = self.stack.pop_2()?;

        let len = Self::cast_to_usize(&len, EvmError::InvalidOperandOOG.into())?;
        self.gas.spend_gas_and_native(0, KECCAK256_NATIVE_COST)?;

        let hash = if len == 0 {
            self.gas.spend_gas(gas_constants::SHA3)?;
            Self::EMPTY_SLICE_SHA3
        } else {
            let memory_offset =
                Self::cast_to_usize(&memory_offset, EvmError::InvalidOperandOOG.into())?;

            self.resize_heap(memory_offset, len)?;

            let allocator = system.get_allocator();
            let input = &self.heap[memory_offset..(memory_offset + len)];

            let mut dst = U256Builder::default();
            S::SystemFunctions::keccak256(&input, &mut dst, self.gas.resources_mut(), allocator)
                .map_err(SystemError::from)?;

            let hash = dst.build();

            if Self::PRINT_OPCODES {
                use core::fmt::Write;
                use zk_ee::logger_log;
                use zk_ee::system::logger::Logger;
                let mut logger = system.get_logger();
                let input = &self.heap()[memory_offset..(memory_offset + len)];
                let input_iter = input.iter().copied();
                logger_log!(logger, " input: ",);
                let _ = logger.log_data(input_iter);
                logger_log!(logger, " -> 0x{hash:0x}");
            }

            hash
        };

        self.stack.push(&hash)
```
