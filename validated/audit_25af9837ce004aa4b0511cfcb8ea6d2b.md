### Title
Unbounded Oracle-Supplied `pubdata_limit` Disables Block-Level Pubdata Cap, Enabling Chain-Halting Pubdata Overflow — (File: `zk_ee/src/system/metadata/zk_metadata.rs`)

---

### Summary

`BlockMetadataFromOracle.pubdata_limit` is deserialized from the oracle with no upper-bound validation. When the prover (oracle-data-influencing caller) supplies `pubdata_limit = u64::MAX`, the block-level pubdata guard in `check_for_block_limits` becomes a no-op (`pubdata_used > u64::MAX` is always false). An unprivileged user can then fill the block with pubdata-heavy transactions, producing a block whose pubdata volume exceeds L1 data-availability capacity and cannot be finalized, halting the chain.

---

### Finding Description

`BlockMetadataFromOracle` is the struct that carries every block-level pricing and limit parameter into the ZKsync OS state-transition function. It is deserialized verbatim from the oracle with no field-level validation:

```rust
// zk_ee/src/system/metadata/zk_metadata.rs  lines 268-301
impl UsizeDeserializable for BlockMetadataFromOracle {
    fn from_iter(src: &mut impl ExactSizeIterator<Item = usize>) -> Result<Self, InternalError> {
        let eip1559_basefee = UsizeDeserializable::from_iter(src)?;
        let pubdata_price   = UsizeDeserializable::from_iter(src)?;
        let native_price    = UsizeDeserializable::from_iter(src)?;
        ...
        let gas_limit       = UsizeDeserializable::from_iter(src)?;
        let pubdata_limit   = UsizeDeserializable::from_iter(src)?;   // ← no bounds check
        ...
        Ok(Self { ..., gas_limit, pubdata_limit, ... })
    }
}
```

The block-level pubdata guard is:

```rust
// basic_bootloader/src/bootloader/block_flow/zk/mod.rs  lines 77-83
} else if !cfg!(feature = "resources_for_tester")
    && pubdata_used > system.get_pubdata_limit()
{
    Err(InvalidTransaction::BlockPubdataLimitReached)
```

`system.get_pubdata_limit()` returns `self.block_level.pubdata_limit` unchanged. When `pubdata_limit = u64::MAX`, the comparison `pubdata_used > u64::MAX` is always `false` for any `u64` accumulator, so the guard never fires.

**Contrast with `gas_limit`**: the gas-limit path has an explicit ceiling enforced during per-transaction validation:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs  lines 84-88
require!(
    block_gas_limit <= MAX_BLOCK_GAS_LIMIT,   // u64::MAX / 256
    InvalidTransaction::BlockGasLimitTooHigh,
    system
)?;
```

No equivalent ceiling exists for `pubdata_limit`. The hardcoded `MAX_NATIVE_COMPUTATIONAL` (`1 << 35`) similarly protects the native-resource dimension, but pubdata has no hardcoded floor.

The test helper `new_for_test()` ships with `pubdata_limit: u64::MAX`, confirming the field is treated as "unlimited" by default and that no downstream code enforces a tighter bound.

---

### Impact Explanation

With `pubdata_limit = u64::MAX` supplied by the prover oracle, the per-block pubdata accumulator in `check_for_block_limits` is permanently bypassed. Any number of pubdata-heavy transactions (e.g., contracts that write many distinct storage slots) can be included in a single block. The resulting pubdata blob will exceed the L1 data-availability budget (EIP-4844 blob capacity or calldata limit), making the block impossible to finalize on L1. The chain halts until a new block is produced that fits within L1 constraints — but since the ZKsync OS STF accepted the oversized block as valid, the sequencer and prover are in an inconsistent state.

---

### Likelihood Explanation

In the proving system the prover supplies `BlockMetadataFromOracle` as non-deterministic oracle input. Because the ZKsync OS STF performs no bounds check on `pubdata_limit`, the ZK proof is valid for any value of that field, including `u64::MAX`. A malicious prover (oracle-data-influencing caller, explicitly listed as in-scope) can therefore set `pubdata_limit = u64::MAX` without invalidating the proof. Once the limit is disabled, any user submitting storage-write-heavy transactions fills the block beyond L1 capacity. The test default of `pubdata_limit: u64::MAX` shows this path is already exercised in practice.

---

### Recommendation

1. **Short term**: Add a hardcoded ceiling in `BlockMetadataFromOracle::from_iter` (or immediately after deserialization in the bootloader's metadata-init step) that rejects or clamps `pubdata_limit` to a protocol-defined maximum (e.g., the maximum bytes that fit in one EIP-4844 blob set, or a constant analogous to `MAX_NATIVE_COMPUTATIONAL`).

2. **Long term**: Audit all oracle-supplied fields in `BlockMetadataFromOracle` for missing range checks. Apply the same pattern already used for `gas_limit` (validated against `MAX_BLOCK_GAS_LIMIT`) and `MAX_NATIVE_COMPUTATIONAL` (hardcoded) to every resource-limit field, so no single oracle-supplied value can silently disable a safety guard.

---

### Proof of Concept

**Step 1 — Prover supplies unbounded `pubdata_limit`.**
The prover sets `pubdata_limit = u64::MAX` in the oracle response for `BLOCK_METADATA_QUERY_ID`. `BlockMetadataFromOracle::from_iter` accepts it without error. [1](#0-0) 

**Step 2 — Block-level pubdata guard is permanently disabled.**
`check_for_block_limits` evaluates `pubdata_used > u64::MAX`, which is always `false`. [2](#0-1) 

**Step 3 — No per-transaction ceiling exists for `pubdata_limit`.**
Unlike `gas_limit`, which is capped at `MAX_BLOCK_GAS_LIMIT` during transaction validation, `pubdata_limit` has no analogous check. [3](#0-2) [4](#0-3) 

**Step 4 — Unprivileged users fill the block with pubdata.**
Any user submitting transactions that write many distinct storage slots (each write contributes 32 + diff bytes of pubdata) can accumulate pubdata far beyond the L1 data-availability limit within a single block. The block is accepted by the STF but cannot be posted to L1, halting the chain. [5](#0-4) 

**Confirming evidence — test default ships with `pubdata_limit = u64::MAX`.** [6](#0-5)

### Citations

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L205-221)
```rust
impl BlockMetadataFromOracle {
    pub fn new_for_test() -> Self {
        BlockMetadataFromOracle {
            eip1559_basefee: U256::from(1000u64),
            pubdata_price: U256::from(0u64),
            native_price: U256::from(10),
            block_number: 1,
            timestamp: 42,
            chain_id: 37,
            gas_limit: u64::MAX / 256,
            pubdata_limit: u64::MAX,
            coinbase: B160::ZERO,
            block_hashes: BlockHashes::default(),
            mix_hash: U256::ONE,
            blob_fee: U256::ZERO,
        }
    }
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L268-301)
```rust
impl UsizeDeserializable for BlockMetadataFromOracle {
    const USIZE_LEN: usize = <Self as UsizeSerializable>::USIZE_LEN;

    fn from_iter(src: &mut impl ExactSizeIterator<Item = usize>) -> Result<Self, InternalError> {
        let eip1559_basefee = UsizeDeserializable::from_iter(src)?;
        let pubdata_price = UsizeDeserializable::from_iter(src)?;
        let native_price = UsizeDeserializable::from_iter(src)?;
        let block_number = UsizeDeserializable::from_iter(src)?;
        let timestamp = UsizeDeserializable::from_iter(src)?;
        let chain_id = UsizeDeserializable::from_iter(src)?;
        let gas_limit = UsizeDeserializable::from_iter(src)?;
        let pubdata_limit = UsizeDeserializable::from_iter(src)?;
        let coinbase = UsizeDeserializable::from_iter(src)?;
        let block_hashes = UsizeDeserializable::from_iter(src)?;
        let mix_hash = UsizeDeserializable::from_iter(src)?;
        let blob_fee = UsizeDeserializable::from_iter(src)?;

        let new = Self {
            eip1559_basefee,
            pubdata_price,
            native_price,
            block_number,
            timestamp,
            chain_id,
            gas_limit,
            pubdata_limit,
            coinbase,
            block_hashes,
            mix_hash,
            blob_fee,
        };

        Ok(new)
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L77-83)
```rust
    } else if !cfg!(feature = "resources_for_tester") && pubdata_used > system.get_pubdata_limit() {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block pubdata limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockPubdataLimitReached)
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L82-94)
```rust
            let block_gas_limit = system.get_gas_limit();
            // First, check block gas limit can be represented as ergs.
            require!(
                block_gas_limit <= MAX_BLOCK_GAS_LIMIT,
                InvalidTransaction::BlockGasLimitTooHigh,
                system
            )?;
            require!(
                tx_gas_limit <= block_gas_limit,
                InvalidTransaction::CallerGasLimitMoreThanBlock,
                system
            )?;
        }
```

**File:** zk_ee/src/system/constants.rs (L26-26)
```rust
pub const MAX_NATIVE_COMPUTATIONAL: u64 = 1 << 35;
```

**File:** basic_system/src/system_implementation/flat_storage_model/storage_cache.rs (L265-308)
```rust
    pub fn calculate_pubdata_used_by_tx(&self) -> u32 {
        let mut visited_elements = BTreeSet::new_in(self.0.alloc.clone());

        let mut pubdata_used = 0u32;
        for element_history in self.0.cache.iter_altered_since_commit() {
            // Elements are sorted chronologically

            let element_key = element_history.key();

            // we publish preimages for account details, so no need to publish hash
            if element_key.address == ACCOUNT_PROPERTIES_STORAGE_ADDRESS {
                continue;
            }

            // Skip if already calculated pubdata for this element
            if visited_elements.contains(element_key) {
                continue;
            }
            visited_elements.insert(element_key);

            let current_value = element_history.current().value();
            let initial_value = element_history.initial().value();
            let at_tx_start_value = element_history.committed().value();

            // If the current value is resetting to the initial one,
            // we don't consider this diff in the pubdata charging.
            // This change will be optimized away, so it's actually reducing
            // pubdata.
            if current_value == initial_value {
                continue;
            }

            if at_tx_start_value != current_value {
                // TODO(EVM-1074): use tree index instead of key for repeated writes
                pubdata_used += 32; // key
                pubdata_used += ValueDiffCompressionStrategy::optimal_compression_length(
                    at_tx_start_value,
                    current_value,
                ) as u32;
            }
        }

        pubdata_used
    }
```
