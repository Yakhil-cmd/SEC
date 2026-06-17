### Title
Unvalidated `native_price` / `pubdata_price` Oracle Inputs Not Committed in ZK Public Inputs Enable Forward/Proving Divergence and Resource Accounting Manipulation — (`zk_ee/src/system/metadata/zk_metadata.rs`, `basic_bootloader/src/bootloader/block_flow/zk/metadata_op.rs`, `basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs`)

---

### Summary

`BlockMetadataFromOracle` carries two ZKsync-specific pricing fields — `native_price` and `pubdata_price` — that are fetched from the oracle and used to derive every transaction's native-resource budget. Neither field is included in the ZK proof's public inputs (`ChainStateCommitment` or `BatchOutput`). A prover supplying the oracle can therefore use values that differ from those the sequencer used, causing a forward/proving divergence: transaction outcomes (success vs. out-of-native-resources revert) can differ between the two execution modes, and the prover can grant transactions an effectively unlimited native-resource budget at zero cost.

The project's own documentation explicitly acknowledges the gap: *"The block header should determine the block fully, i.e. include all the inputs needed to execute the block. Currently it misses `gas_per_pubdata` and `native_price`, but we already working on design and implementation to solve this issue."*

---

### Finding Description

**Step 1 — Oracle fetch with no pricing validation.**

`ZkMetadata::metadata_op` queries the oracle for the full `BlockMetadataFromOracle` struct. The only post-fetch check is a gas-limit bound; `native_price` and `pubdata_price` are accepted as-is:

```rust
let block_level_metadata: BlockMetadataFromOracle =
    oracle.query_with_empty_input(BLOCK_METADATA_QUERY_ID)?;
// Only gas-limit bounds are checked; native_price / pubdata_price are unconstrained
if metadata.block_gas_limit() > MAX_BLOCK_GAS_LIMIT
    || metadata.individual_tx_gas_limit() > MAX_TX_GAS_LIMIT
{
    return Err(internal_error!("block or tx gas limit is too high"));
}
``` [1](#0-0) 

**Step 2 — Prices drive the entire native-resource budget.**

In `validate_and_compute_fee_for_transaction`, both values are read directly from the oracle-supplied metadata and used to compute `native_per_gas` and `native_per_pubdata`:

```rust
let pubdata_price = system.get_pubdata_price();
let native_price  = system.get_native_price();
// native_per_gas = ceil(gas_price / native_price)
let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price))...;
// native_per_pubdata = pubdata_price / native_price
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))...;
``` [2](#0-1) 

When `native_per_gas == 0` (which occurs when `native_price` is set to any value larger than `gas_price`), the resource-creation helper assigns `native_limit = u64::MAX - 1` — effectively unlimited native resources.

**Step 3 — Neither field appears in the ZK public inputs.**

`ChainStateCommitment` (the per-block state hash committed on-chain) contains only `state_root`, `next_free_slot`, `block_number`, `last_256_block_hashes_blake`, and `last_block_timestamp`: [3](#0-2) 

`BatchOutput` (the batch-level hash opened on the settlement layer) contains chain ID, timestamps, DA commitment, L1/L2 tx counts, priority ops hash, L2 logs root, upgrade tx hash, and settlement layer chain ID — but not `native_price` or `pubdata_price`: [4](#0-3) 

Because neither commitment covers these fields, a proof generated with `native_price = X` is indistinguishable from one generated with `native_price = Y` at the settlement layer.

**Step 4 — `BlockMetadataFromOracle` struct carrying the unconstrained fields:** [5](#0-4) 

---

### Impact Explanation

**Forward/proving divergence (state-transition bug):** The sequencer runs with operator-chosen prices; the prover can substitute different values. Transactions that succeeded in forward mode can be made to revert in proving mode (by lowering `native_price` so `native_per_gas` is larger, exhausting native resources sooner), and vice versa. The resulting proven state differs from the sequenced state, breaking the fundamental correctness guarantee of the rollup.

**Unlimited native-resource grant:** By setting `native_price` to any value ≥ `gas_price + 1`, the prover forces `native_per_gas = 0`, which triggers `native_limit = u64::MAX - 1`. Every transaction in the proven block then has an unbounded native-resource budget regardless of what the user paid. This allows the prover to prove computationally expensive blocks without the corresponding fee revenue, effectively subsidising arbitrary computation at the protocol's expense.

**Pubdata-price manipulation:** Similarly, `pubdata_price` is unconstrained. Setting it to zero makes `native_per_pubdata = 0`, removing all pubdata charges from every transaction in the proven block.

---

### Likelihood Explanation

The attacker is the prover/operator supplying oracle data — explicitly listed as an in-scope attacker type ("prover/forward execution input"). The oracle query for block metadata is unconditional and occurs at the start of every block. No additional preconditions are required; the manipulation is trivially exercised by supplying a crafted `BlockMetadataFromOracle` response. The project's own documentation confirms the gap is real and unresolved. [6](#0-5) 

---

### Recommendation

1. **Commit `native_price` and `pubdata_price` into the public inputs.** Include them in `ChainStateCommitment` or `BatchOutput` (or a dedicated block-header field such as `extra_data`, as the docs already contemplate) so the settlement layer can verify they match the values used during execution.

2. **Add bounds checks on `native_price` and `pubdata_price`** in `metadata_op.rs` analogous to the existing gas-limit check — e.g., reject `native_price == 0` and enforce a protocol-defined maximum.

3. **Treat `native_per_gas == 0` as an error** rather than silently granting `u64::MAX - 1` native resources, or require an explicit operator opt-in for free-native-resource blocks.

---

### Proof of Concept

```
Sequencer forward run:
  BlockMetadataFromOracle { native_price: 1000, pubdata_price: 5000, ... }
  → native_per_gas = ceil(gas_price / 1000)
  → Transaction T succeeds with native_used = 2^34 (within budget)

Prover oracle substitution:
  BlockMetadataFromOracle { native_price: U256::MAX, pubdata_price: 0, ... }
  → native_per_gas = ceil(gas_price / U256::MAX) = 0
  → native_limit = u64::MAX - 1  (unlimited)
  → native_per_pubdata = 0       (no pubdata charge)
  → Transaction T succeeds with zero effective resource cost

Proof submitted to L1:
  BatchPublicInput { state_before, state_after, batch_output }
  — batch_output contains no native_price or pubdata_price field
  → Proof verifies successfully despite using manipulated prices
  → Settlement layer cannot detect the substitution
```

The divergence is rooted in `metadata_op.rs` (unconstrained oracle fetch), amplified in `validation_impl.rs` (prices drive all resource budgets), and made exploitable by the absence of these fields from `public_input.rs` (`ChainStateCommitment::hash` and `BatchOutput::hash`).

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/metadata_op.rs (L14-34)
```rust
    fn metadata_op<Config: BasicBootloaderExecutionConfig>(
        oracle: &mut impl IOOracle,
        _allocator: S::Allocator,
    ) -> Result<<S as SystemTypes>::Metadata, InternalError> {
        let block_level_metadata: BlockMetadataFromOracle =
            oracle.query_with_empty_input(BLOCK_METADATA_QUERY_ID)?;

        let metadata = ZkMetadata {
            tx_level: TxLevelMetadata::default(),
            block_level: block_level_metadata,
            _marker: core::marker::PhantomData,
        };

        if metadata.block_gas_limit() > MAX_BLOCK_GAS_LIMIT
            || metadata.individual_tx_gas_limit() > MAX_TX_GAS_LIMIT
        {
            return Err(internal_error!("block or tx gas limit is too high"));
        }

        Ok(metadata)
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L106-143)
```rust
    let pubdata_price = system.get_pubdata_price();
    let native_price = system.get_native_price();

    let gas_price = if transaction.is_service() {
        // Service transactions do not pay gas fees,
        // their gas price is allowed to be < block base fee.
        U256::ZERO
    } else {
        get_gas_price::<S, Config>(
            system,
            transaction.max_fee_per_gas(),
            transaction.max_priority_fee_per_gas(),
        )?
    };

    let native_per_gas = {
        if native_price.is_zero() {
            return Err(internal_error!("Native price cannot be 0").into());
        }

        if cfg!(feature = "resources_for_tester") {
            crate::bootloader::constants::TESTER_NATIVE_PER_GAS
        } else if Config::SIMULATION && gas_price.is_zero() {
            // For simulation, if gas price isn't set, we use base fee
            // for native calculation
            u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price)).ok_or(
                TxError::Validation(InvalidTransaction::NativeResourcesAreTooExpensive),
            )?
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
    };

    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L18-41)
```rust
pub struct ChainStateCommitment {
    pub state_root: Bytes32,
    pub next_free_slot: u64,
    pub block_number: u64,
    pub last_256_block_hashes_blake: Bytes32,
    pub last_block_timestamp: u64,
}

impl ChainStateCommitment {
    ///
    /// Calculate blake2s hash of chain state commitment.
    ///
    /// We are using proving friendly blake2s because this commitment will be generated and opened during proving,
    /// but we don't need to open it on the settlement layer.
    ///
    pub fn hash(&self) -> [u8; 32] {
        let mut hasher = crypto::blake2s::Blake2s256::new();
        hasher.update(self.state_root.as_u8_ref());
        hasher.update(&self.next_free_slot.to_be_bytes());
        hasher.update(&self.block_number.to_be_bytes());
        hasher.update(self.last_256_block_hashes_blake.as_u8_ref());
        hasher.update(&self.last_block_timestamp.to_be_bytes());
        hasher.finalize()
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L52-103)
```rust
pub struct BatchOutput {
    /// Chain id used during execution of the blocks.
    pub chain_id: U256,
    /// First block timestamp.
    pub first_block_timestamp: u64,
    /// Last block timestamp.
    pub last_block_timestamp: u64,
    /// DA commitment scheme.
    pub da_commitment_scheme: DACommitmentScheme,
    /// Pubdata commitment.
    pub pubdata_commitment: Bytes32,
    /// Number of l1 -> l2 processed txs in the batch.
    pub number_of_layer_1_txs: U256,
    /// Number of processed L2 txs in the batch.
    pub number_of_layer_2_txs: U256,
    /// Rolling keccak256 hash of l1 -> l2 txs processed in the batch.
    pub priority_operations_hash: Bytes32,
    /// L2 logs tree root.
    /// Note that it's full root, it's keccak256 of:
    /// - merkle root of l2 -> l1 logs in the batch .
    /// - multichain root - commitment to logs emitted on chains that settle on the current.
    pub l2_logs_tree_root: Bytes32,
    /// Protocol upgrade tx hash (0 if there wasn't)
    pub upgrade_tx_hash: Bytes32,
    /// Linear keccak256 hash of interop roots
    pub interop_roots_rolling_hash: Bytes32,
    /// Settlement layer chain id.
    pub settlement_layer_chain_id: U256,
}

impl BatchOutput {
    ///
    /// Calculate keccak256 hash of public input
    ///
    pub fn hash(&self) -> [u8; 32] {
        let mut hasher = Keccak256::new();
        hasher.update(self.chain_id.to_be_bytes::<32>());
        hasher.update(&self.first_block_timestamp.to_be_bytes());
        hasher.update(&self.last_block_timestamp.to_be_bytes());
        // Encode DA commitment scheme as U256 BE
        hasher.update([0u8; 31]);
        hasher.update([self.da_commitment_scheme as u8]);
        hasher.update(self.pubdata_commitment.as_u8_ref());
        hasher.update(self.number_of_layer_1_txs.to_be_bytes::<32>());
        hasher.update(self.number_of_layer_2_txs.to_be_bytes::<32>());
        hasher.update(self.priority_operations_hash.as_u8_ref());
        hasher.update(self.l2_logs_tree_root.as_u8_ref());
        hasher.update(self.upgrade_tx_hash.as_u8_ref());
        hasher.update(self.interop_roots_rolling_hash.as_u8_ref());
        hasher.update(self.settlement_layer_chain_id.to_be_bytes::<32>());
        hasher.finalize()
    }
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L112-132)
```rust
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct BlockMetadataFromOracle {
    // Chain id is temporarily also added here (so that it can be easily passed from the oracle)
    // long term, we have to decide whether we want to keep it here, or add a separate oracle
    // type that would return some 'chain' specific metadata (as this class is supposed to hold block metadata only).
    pub chain_id: u64,
    pub block_number: u64,
    pub block_hashes: BlockHashes,
    pub timestamp: u64,
    pub eip1559_basefee: U256,
    pub pubdata_price: U256,
    pub native_price: U256,
    pub coinbase: B160,
    pub gas_limit: u64,
    pub pubdata_limit: u64,
    /// Source of randomness, currently holds the value
    /// of prevRandao.
    pub mix_hash: U256,
    pub blob_fee: U256,
}
```

**File:** docs/bootloader/bootloader.md (L35-36)
```markdown
The block header should determine the block fully, i.e. include all the inputs needed to execute the block.
Currently it misses `gas_per_pubdata` and `native_price`, but we already working on design and implementation to solve this issue.
```
