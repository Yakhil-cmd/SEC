### Title
Missing `block_number >= 1` Validation Before Subtraction Produces Wrong `ChainStateCommitment` Public Input - (`basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs` and `post_tx_op_proving_multiblock_batch.rs`)

---

### Summary

Both single-block and multi-block proving post-ops compute `chain_state_commitment_before` using `metadata.block_number() - 1` without first asserting `block_number >= 1`. For the genesis block (`block_number = 0`), this subtraction wraps to `u64::MAX` in release-mode Rust (the RISC-V proving binary), producing a cryptographically wrong public-input hash that the settlement layer will reject, making the genesis block permanently unprovable.

---

### Finding Description

In both proving post-op implementations, the "before" chain state commitment is constructed as:

```rust
let chain_state_commitment_before = ChainStateCommitment {
    state_root: state_commitment.root,
    next_free_slot: state_commitment.next_free_slot,
    block_number: metadata.block_number() - 1,   // ← unchecked subtraction
    last_256_block_hashes_blake: blocks_hasher.finalize().into(),
    last_block_timestamp,
};
``` [1](#0-0) [2](#0-1) 

The `block_number` field is read directly from `BlockMetadataFromOracle` supplied by the oracle, and the only validation performed at metadata initialization time is a gas-limit range check — there is no lower-bound check on `block_number`:

```rust
if metadata.block_gas_limit() > MAX_BLOCK_GAS_LIMIT
    || metadata.individual_tx_gas_limit() > MAX_TX_GAS_LIMIT
{
    return Err(internal_error!("block or tx gas limit is too high"));
}
``` [3](#0-2) 

By contrast, the analogous timestamp ordering invariant **is** enforced with an explicit assertion:

```rust
// validate that timestamp didn't decrease
assert!(metadata.block_timestamp() >= last_block_timestamp);
``` [4](#0-3) 

No equivalent guard exists for `block_number >= 1` before the subtraction. The `ChainStateCommitment` struct documents that `last_block_timestamp` is included "to ensure that block timestamps are not decreasing," but no parallel guarantee is documented or enforced for `block_number`. [5](#0-4) 

The `block_number` field of `BlockMetadataFromOracle` is a plain `u64` with no invariant attached: [6](#0-5) 

The genesis block is confirmed to use `block_number = 0` in the test suite: [7](#0-6) 

---

### Impact Explanation

When `block_number = 0`, the expression `metadata.block_number() - 1` wraps to `u64::MAX` in Rust release mode (the RISC-V proving binary is compiled without overflow checks). The resulting `chain_state_commitment_before` hash — which is committed to the settlement layer as the "before" state — will contain `block_number = u64::MAX` instead of the correct genesis sentinel. The settlement layer will reject the proof because the claimed "before" commitment does not match the stored genesis state commitment. The genesis block becomes permanently unprovable, halting the chain from its very first block.

**Vulnerability class:** State-transition bug / valid-execution unprovability — the block executes correctly in forward mode but the proof carries a wrong public input.

---

### Likelihood Explanation

Every new chain deployment that starts from `block_number = 0` (the standard genesis) will hit this path deterministically. The bug is unconditional: no special transaction content or attacker action is required beyond the sequencer submitting the first block for proving.

---

### Recommendation

Add an explicit guard before the subtraction in both proving post-ops, mirroring the existing timestamp assertion:

```rust
// Analogous to the timestamp non-decrease assertion
assert!(metadata.block_number() >= 1,
    "block_number must be >= 1 for proving; genesis block cannot use this path");
let chain_state_commitment_before = ChainStateCommitment {
    ...
    block_number: metadata.block_number() - 1,
    ...
};
```

Alternatively, use `saturating_sub(1)` and document the genesis-block handling contract explicitly, or introduce a dedicated genesis-block proving path that sets `block_number = 0` in the "before" commitment.

---

### Proof of Concept

1. Deploy a new ZKsync OS chain. The first block has `block_number = 0` (confirmed by the test suite).
2. Execute the first block in forward mode — it succeeds normally.
3. Invoke the proving path (`ZKHeaderStructurePostTxOpProvingSingleblockBatch::post_op`).
4. Inside `post_op`, `metadata.block_number()` returns `0`.
5. `metadata.block_number() - 1` wraps to `18446744073709551615` (`u64::MAX`) in release mode.
6. `chain_state_commitment_before` is hashed with `block_number = u64::MAX`.
7. The public input hash is submitted to the settlement layer.
8. The settlement layer compares against the stored genesis commitment (which has `block_number = 0`) — mismatch → proof rejected.
9. The chain cannot advance past block 0. [8](#0-7) [9](#0-8)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs (L120-146)
```rust
        let (mut state_commitment, last_block_timestamp) = {
            let proof_data: ProofData<FlatStorageCommitment<TREE_HEIGHT>> =
                ZKProofDataQuery::get(&mut io.oracle, &())
                    .expect("must get proof data from oracle");
            (proof_data.state_root_view, proof_data.last_block_timestamp)
        };

        logger_log!(
            logger,
            "Initial state commitment is {:?}\n",
            &state_commitment
        );
        // validate that timestamp didn't decrease
        assert!(metadata.block_timestamp() >= last_block_timestamp);

        // chain state commitment before
        let mut blocks_hasher = Blake2s256::new();
        for block_hash in metadata.block_level.block_hashes.0.iter() {
            blocks_hasher.update(&block_hash.to_be_bytes::<32>());
        }
        let chain_state_commitment_before = ChainStateCommitment {
            state_root: state_commitment.root,
            next_free_slot: state_commitment.next_free_slot,
            block_number: metadata.block_number() - 1,
            last_256_block_hashes_blake: blocks_hasher.finalize().into(),
            last_block_timestamp,
        };
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs (L135-141)
```rust
        let chain_state_commitment_before = ChainStateCommitment {
            state_root: state_commitment.root,
            next_free_slot: state_commitment.next_free_slot,
            block_number: metadata.block_number() - 1,
            last_256_block_hashes_blake: blocks_hasher.finalize().into(),
            last_block_timestamp,
        };
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/metadata_op.rs (L27-31)
```rust
        if metadata.block_gas_limit() > MAX_BLOCK_GAS_LIMIT
            || metadata.individual_tx_gas_limit() > MAX_TX_GAS_LIMIT
        {
            return Err(internal_error!("block or tx gas limit is too high"));
        }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L8-24)
```rust
/// Commitment to state that we need to keep between blocks execution:
/// - state commitment(`state_root` and `next_free_slot`)
/// - block number
/// - last 256 block hashes, previous can be "unrolled" from the last, but we commit to 256 for optimization.
/// - last block timestamp, to ensure that block timestamps are not decreasing.
///
/// This commitment(hash of its fields) will be saved on the settlement layer.
/// With proofs, we'll ensure that the values used during block execution correspond to this commitment.
///
#[derive(Debug)]
pub struct ChainStateCommitment {
    pub state_root: Bytes32,
    pub next_free_slot: u64,
    pub block_number: u64,
    pub last_256_block_hashes_blake: Bytes32,
    pub last_block_timestamp: u64,
}
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L134-142)
```rust
impl BasicBlockMetadata<EthereumIOTypesConfig> for BlockMetadataFromOracle {
    fn chain_id(&self) -> u64 {
        self.chain_id
    }

    fn block_number(&self) -> u64 {
        self.block_number
    }

```

**File:** tests/instances/header/src/lib.rs (L27-27)
```rust
    assert_eq!(header.number, 0);
```

**File:** zk_ee/src/common_structs/proof_data.rs (L8-19)
```rust
///
/// During proof run we need extra data to validate provided inputs against chain state commitment before the block.
///
/// We'll validate reads/apply writes against `state_root_view` and validate that block timestamp is greater than `last_block_timestamp`.
/// At the end we'll calculate chain state commitment before using this fields and other metadata values(block number, hashes) used during execution.
///
#[derive(Clone, Copy, Debug)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
pub struct ProofData<SR: StateRootView<EthereumIOTypesConfig>> {
    pub state_root_view: SR,
    pub last_block_timestamp: u64,
}
```
