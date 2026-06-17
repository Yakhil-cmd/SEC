### Title
Non-Strict Timestamp Monotonicity Allows Repeated Block Timestamps, Breaking Time-Sensitive EVM Contracts - (File: `basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs` and `post_tx_op_proving_multiblock_batch.rs`)

---

### Summary

ZKsync OS enforces only a non-strict (`>=`) monotonicity check on block timestamps during proving. This means consecutive blocks are permitted to carry identical timestamps. Any EVM contract deployed on ZKsync OS that uses `block.timestamp` (the `TIMESTAMP` opcode) to measure elapsed time or enforce time-based logic can be broken: the operator can produce an arbitrary number of blocks with the same timestamp, causing time to appear frozen from the contract's perspective.

---

### Finding Description

In both the single-block and multi-block proving post-transaction operations, the only timestamp invariant enforced is:

```rust
// validate that timestamp didn't decrease
assert!(metadata.block_timestamp() >= last_block_timestamp);
```

This check is present identically in both:
- `post_tx_op_proving_singleblock_batch.rs`, line 133
- `post_tx_op_proving_multiblock_batch.rs`, line 128

The `>=` operator permits `metadata.block_timestamp() == last_block_timestamp`, meaning a new block can be produced with the exact same timestamp as the previous block. There is no lower bound enforcing strict increase (`>`).

The `TIMESTAMP` opcode in the EVM interpreter dispatches directly to `system.get_timestamp()`, which returns `metadata.block_timestamp()` — the value set by the operator in the oracle-provided `BlockMetadataFromOracle`. There is no additional validation of the timestamp value at the metadata initialization stage (`zk/metadata_op.rs`) beyond the gas limit checks.

The `ChainStateCommitment` structure stores `last_block_timestamp` specifically "to ensure that block timestamps are not decreasing," but the comment itself acknowledges only non-decrease, not strict increase. This committed value is what the prover checks against, so the proof system itself accepts equal timestamps across blocks.

The block timestamp is fully operator-controlled: it is read from the oracle via `BLOCK_METADATA_QUERY_ID` with no on-chain enforcement of a minimum increment. The only external constraint is the settlement-layer validation of `first_block_timestamp` and `last_block_timestamp` in `BatchOutput`, but within a batch, individual block timestamps can all be identical.

---

### Impact Explanation

Any EVM contract deployed on ZKsync OS that relies on `block.timestamp` for time-based logic is vulnerable to time-freezing. Concrete examples:

- **Lending/borrowing protocols** that accrue interest per second using `block.timestamp` deltas will accrue zero interest across any number of blocks with the same timestamp.
- **Vesting contracts** that release tokens after a timestamp threshold will never release if the operator keeps the timestamp frozen below the threshold.
- **Auction contracts** with timestamp-based deadlines can be manipulated: the operator can freeze time to prevent an auction from ending.
- **TWAP oracles** that compute time-weighted averages using `block.timestamp` will produce incorrect prices when time is frozen.

Unlike Ethereum, where each block's timestamp must be strictly greater than its parent's (enforced by consensus), ZKsync OS has no such strict enforcement. The `TIMESTAMP` opcode returns the operator-supplied value with no minimum-increment guarantee visible to contracts.

---

### Likelihood Explanation

The operator controls the `BlockMetadataFromOracle` input to the oracle. The timestamp field is set externally with no protocol-level enforcement of a minimum per-block increment. The only constraint is that the timestamp must not decrease across blocks (non-strict monotonicity). A sequencer/operator can trivially produce multiple blocks with the same timestamp. This is a structural property of the system, not a rare edge case. Any time-sensitive contract deployed on ZKsync OS is affected by this divergence from Ethereum semantics.

---

### Recommendation

Change the timestamp validation from non-strict to strict monotonicity:

```rust
// Before (non-strict):
assert!(metadata.block_timestamp() >= last_block_timestamp);

// After (strict):
assert!(metadata.block_timestamp() > last_block_timestamp);
```

Apply this change in both:
- `basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs`, line 133
- `basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs`, line 128

Additionally, document this divergence from Ethereum semantics in `docs/execution_environments/evm.md` (the existing "Current divergences" section), so developers are aware that `block.timestamp` is not guaranteed to strictly increase between blocks.

---

### Proof of Concept

1. Deploy a simple interest-accrual contract on ZKsync OS:
   ```solidity
   contract Vault {
       uint256 public lastUpdate;
       uint256 public accruedInterest;
       uint256 constant RATE = 1e18; // 1 token per second
       
       function accrue() external {
           uint256 elapsed = block.timestamp - lastUpdate;
           accruedInterest += elapsed * RATE;
           lastUpdate = block.timestamp;
       }
   }
   ```

2. Call `accrue()` in block N with timestamp T.

3. The operator produces block N+1 with the same timestamp T (permitted by `assert!(metadata.block_timestamp() >= last_block_timestamp)` at line 133 of `post_tx_op_proving_singleblock_batch.rs`).

4. Call `accrue()` in block N+1. `elapsed = T - T = 0`, so `accruedInterest` does not increase despite a new block having been produced.

5. The proof for block N+1 is valid because the prover only checks `metadata.block_timestamp() >= last_block_timestamp` (line 128 of `post_tx_op_proving_multiblock_batch.rs`), which is satisfied when both are equal.

The operator can repeat step 3–4 indefinitely, freezing time for all contracts in the system. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs (L132-133)
```rust
        // validate that timestamp didn't decrease
        assert!(metadata.block_timestamp() >= last_block_timestamp);
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs (L127-128)
```rust
        // validate that timestamp didn't decrease
        assert!(metadata.block_timestamp() >= last_block_timestamp);
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L12-12)
```rust
/// - last block timestamp, to ensure that block timestamps are not decreasing.
```

**File:** evm_interpreter/src/interpreter.rs (L279-280)
```rust
                    opcodes::TIMESTAMP => self.timestamp(system),
                    opcodes::NUMBER => self.number(system),
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/metadata_op.rs (L27-31)
```rust
        if metadata.block_gas_limit() > MAX_BLOCK_GAS_LIMIT
            || metadata.individual_tx_gas_limit() > MAX_TX_GAS_LIMIT
        {
            return Err(internal_error!("block or tx gas limit is too high"));
        }
```

**File:** docs/execution_environments/evm.md (L8-15)
```markdown
## Current divergences

- Keyless transactions may not work, more generally, we have additional cost due to pubdata.
- Deployment doesn’t fail if the storage for the deployed address is already used (when nonce is 0 and code is empty).
- When the block base fee is 0, then priority fee from transactions is ignored. That is, the gas price will also be 0 for every transaction.
- DIFFICULTY is mocked (returns 1), we don’t plan to support it
- EIP-4844 blob transactions (type 3) are not enabled in production. BLOBHASH always returns 0 (no blob hashes available). BLOBBASEFEE returns the value from block metadata.
- Blake2F precompile is not enabled in production
```
