### Title
Integer Underflow in `eip2935_system_part` at Genesis Block — (`File: basic_bootloader/src/bootloader/block_flow/ethereum/eip_2935_historical_block_hash/mod.rs`)

---

### Summary

`eip2935_system_part` unconditionally computes `block_number - 1` without guarding against `block_number == 0`. At genesis (block 0) with the EIP-2935 history contract pre-deployed, this underflows: in debug/proving builds it panics (making block 0 unprovable), and in release builds it silently wraps to `u64::MAX`, writing the parent hash to the wrong storage slot and corrupting the EIP-2935 history contract state.

---

### Finding Description

`eip2935_system_part` reads the current block number from the oracle-provided metadata and immediately subtracts 1 to obtain the parent block number and the storage slot index:

```rust
let block_number = system.get_block_number();
let parent_hash = system.get_blockhash(block_number - 1)?;   // underflows when block_number == 0
// ...
let slot_idx = (block_number - 1) % HISTORY_SERVE_WINDOW;   // underflows when block_number == 0
``` [1](#0-0) 

There is no guard for `block_number == 0`. The analogous `form_block_header` function in the same codebase explicitly handles this edge case:

```rust
let previous_block_hash = if block_number == 0 {
    Bytes32::ZERO
} else {
    system.get_blockhash(block_number - 1)?
};
``` [2](#0-1) 

This demonstrates that the developers are aware of the genesis-block edge case but did not apply the same guard in `eip2935_system_part`.

The EIP-2935 contract is only invoked when it is already deployed (`nonce == 1 && bytecode_len > 0`): [3](#0-2) 

For a ZKsync OS chain that pre-deploys the EIP-2935 history contract in its genesis state (a standard requirement for EIP-2935-enabled chains), this guard passes and the underflow is reached on the very first block.

`get_blockhash(u64::MAX)` silently returns `Bytes32::ZERO` because `u64::MAX >= current_block_number (0)` triggers the out-of-range path: [4](#0-3) 

So in release mode, `Bytes32::ZERO` is written to slot `u64::MAX % 8191 = 7998` of the EIP-2935 contract instead of no write occurring. In debug/proving mode, the subtraction panics.

---

### Impact Explanation

**State corruption (release/forward execution):** The EIP-2935 history contract receives a spurious write of `Bytes32::ZERO` to slot 7998. This slot legitimately belongs to block 7999's parent hash. When block 7999 is eventually processed, the correct value overwrites it, but any read of slot 7998 between block 0 and block 7999 returns a wrong (zero) value, breaking `BLOCKHASH` lookups for contracts that query historical hashes via the EIP-2935 contract.

**Unprovability / DoS (debug/proving build):** Rust panics on unsigned integer underflow in debug mode. The ZKsync OS proving pipeline compiles and runs the bootloader in an environment where overflow checks may be active. A panic during block 0 execution makes the genesis block unprovable, permanently halting the chain from producing its first proof.

---

### Likelihood Explanation

Any EIP-2935-enabled ZKsync OS chain that pre-deploys the history contract at genesis (the standard deployment model) will hit this on block 0 — the very first block executed. No attacker action is required; normal chain startup is sufficient. The bug is deterministic and 100% reproducible on affected configurations.

---

### Recommendation

Add the same `block_number == 0` guard that `form_block_header` already uses:

```rust
let block_number = system.get_block_number();
if block_number == 0 {
    return Ok(()); // No parent exists at genesis
}
let parent_hash = system.get_blockhash(block_number - 1)?;
let slot_idx = (block_number - 1) % HISTORY_SERVE_WINDOW;
```

---

### Proof of Concept

1. Deploy a ZKsync OS chain with the EIP-2935 history contract pre-deployed in genesis state (`nonce = 1`, `bytecode_len > 0` at `HISTORY_STORAGE_ADDRESS`).
2. Execute block 0.
3. `eip2935_system_part` is called; `is_contract` is `true`; execution reaches `block_number - 1` with `block_number = 0`.
4. **Debug build:** Rust panics with integer overflow → block 0 execution aborts → chain cannot produce its first proof.
5. **Release build:** `block_number - 1` wraps to `u64::MAX`; `get_blockhash(u64::MAX)` returns `Bytes32::ZERO`; `u64::MAX % 8191 = 7998`; `Bytes32::ZERO` is written to slot 7998 of the EIP-2935 contract — a slot that should remain unwritten until block 7999.

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/eip_2935_historical_block_hash/mod.rs (L44-48)
```rust
    let is_contract = props.nonce.0 == 1 && props.observable_bytecode_len.0 > 0;
    if is_contract == false {
        // fail silently
        return Ok(());
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/eip_2935_historical_block_hash/mod.rs (L50-57)
```rust
    let block_number = system.get_block_number();
    let parent_hash = system.get_blockhash(block_number - 1)?;

    system_log!(system, "EIP-2935 parent hash = {:?}\n", &parent_hash);

    let slot_idx = (block_number - 1) % HISTORY_SERVE_WINDOW;
    let mut slot = Bytes32::ZERO;
    slot.as_u8_array_mut()[24..32].copy_from_slice(&slot_idx.to_be_bytes());
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs (L74-79)
```rust
    let block_number = system.get_block_number();
    let previous_block_hash = if block_number == 0 {
        Bytes32::ZERO
    } else {
        system.get_blockhash(block_number - 1)?
    };
```

**File:** zk_ee/src/system/mod.rs (L127-142)
```rust
    pub fn get_blockhash(&self, block_number: u64) -> Result<Bytes32, InternalError> {
        let current_block_number = self.metadata.block_number();
        if block_number >= current_block_number
            || block_number < current_block_number.saturating_sub(256)
        {
            // Out of range
            Ok(Bytes32::ZERO)
        } else {
            let depth = current_block_number - block_number;
            self.metadata
                .block_historical_hash(depth)
                .ok_or(internal_error!(
                    "historical hash of limited depth must be provided"
                ))
        }
    }
```
