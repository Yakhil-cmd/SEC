### Title
Operator-Controlled `native_price`/`pubdata_price` Absent from ZK Proof Public Input Enables Forward/Proving Divergence - (File: `zk_ee/src/system/metadata/zk_metadata.rs`)

### Summary
`BlockMetadataFromOracle` carries `native_price` and `pubdata_price` fields that govern all native-resource and pubdata fee accounting for every transaction in a block. These fields are read from the oracle at proving time but are **never committed to in the ZK proof's public input**. A malicious prover can supply values that differ from those used during forward (sequencer) execution, causing the proven state root to diverge from the state root the sequencer actually produced, while the proof remains cryptographically valid.

### Finding Description

`BlockMetadataFromOracle` is defined in `zk_ee/src/system/metadata/zk_metadata.rs` and contains both `pubdata_price` and `native_price`: [1](#0-0) 

These two fields drive the entire double-resource-accounting model. During L2 transaction validation the bootloader reads them directly: [2](#0-1) 

`native_per_gas = ceil(gas_price / native_price)` and `native_per_pubdata = pubdata_price / native_price` determine the native-resource limit for every transaction. If the limit is exhausted the transaction is **reverted** and all state changes are rolled back.

In the proving system the block metadata is fetched from the CSR-based oracle with no in-circuit constraint: [3](#0-2) 

The public input that is ultimately committed on L1 is computed in `post_tx_op_proving_singleblock_batch.rs`. The `BatchOutput` hash covers `chain_id`, timestamps, DA commitment, L1-tx hash, logs root, and upgrade-tx hash: [4](#0-3) 

The `ChainStateCommitment` covers `state_root`, `next_free_slot`, `block_number`, `last_256_block_hashes_blake`, and `last_block_timestamp`: [5](#0-4) 

**Neither `native_price` nor `pubdata_price` appears in either commitment.** The project documentation explicitly acknowledges this gap: [6](#0-5) 

The oracles documentation states block metadata "is verified by having it as part of the public inputs," but the implementation does not enforce this for the two pricing fields: [7](#0-6) 

### Impact Explanation

A malicious prover executes the following divergence attack:

1. **Forward execution** (sequencer): block is built with `native_price = P`. Some transaction `T` exhausts its native-resource budget and is **reverted**; its storage writes are rolled back. The sequencer's final state root is `R_fwd`.

2. **Proving execution** (prover): the prover injects `native_price = 1` into the CSR oracle response. `native_per_gas` becomes `gas_price`, giving every transaction an enormous native-resource budget. Transaction `T` now **succeeds**; its storage writes are committed. The prover's final state root is `R_prv ≠ R_fwd`.

3. The ZK proof commits to `R_prv`. Because `native_price` is unconstrained, the proof is cryptographically valid.

4. The L1 settlement contract verifies the proof and advances the canonical state to `R_prv`.

The result is that the on-chain state diverges from what the sequencer published to L2 users. The prover can selectively flip transaction outcomes (revert→success or success→revert) for any transaction whose native-resource consumption is sensitive to the pricing ratio, enabling arbitrary state manipulation within a block.

The same attack applies to `pubdata_price`: setting it to zero removes all pubdata charges, allowing transactions that would have been reverted by the post-execution pubdata check to succeed instead. [8](#0-7) 

### Likelihood Explanation

The attacker is the prover, which the scope explicitly lists as a valid threat actor ("prover/forward execution input"). The prover controls the CSR oracle data supplied to the RISC-V binary. No additional privilege beyond operating the prover is required. The manipulation is straightforward: substitute a single field in the serialized `BlockMetadataFromOracle` response. The attack is silent—the proof verifies correctly on L1 and leaves no on-chain evidence of tampering.

### Recommendation

1. **Include `native_price` and `pubdata_price` in the block header** (currently listed as `extra_data` / TBD) so they become part of the block hash that feeds into `ChainStateCommitment`.
2. **Commit them in the public input** (either directly in `BatchOutput` or via the block hash already present in `ChainStateCommitment`) so the ZK circuit constrains the prover to use the same values as the sequencer.
3. Until the above is implemented, the sequencer should publish `native_price` and `pubdata_price` alongside each block so that any divergence between forward and proving execution is externally detectable.

### Proof of Concept

```
// Forward execution (sequencer side)
BlockMetadataFromOracle { native_price: 1_000, pubdata_price: 500_000, ... }
// Transaction T: native_limit = gas_limit * (gas_price / 1_000) → exhausted → REVERT
// State root after block: R_fwd

// Proving execution (malicious prover)
// Prover writes to CSR oracle: native_price = 1, pubdata_price = 0
BlockMetadataFromOracle { native_price: 1, pubdata_price: 0, ... }
// Transaction T: native_limit = gas_limit * gas_price → enormous → SUCCESS
// State root after block: R_prv  (≠ R_fwd, T's writes are now committed)

// ZK proof commits to R_prv; native_price/pubdata_price absent from public input
// L1 verifies proof → accepts R_prv as canonical state
// On-chain state ≠ L2 state published by sequencer
```

### Citations

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L114-132)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L106-139)
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
```

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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L17-41)
```rust
#[derive(Debug)]
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L82-103)
```rust
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

**File:** docs/bootloader/bootloader.md (L35-36)
```markdown
The block header should determine the block fully, i.e. include all the inputs needed to execute the block.
Currently it misses `gas_per_pubdata` and `native_price`, but we already working on design and implementation to solve this issue.
```

**File:** docs/system/io/oracles.md (L11-11)
```markdown
- Reading block metadata (this is verified by having it as part of the public inputs).
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-435)
```rust
pub fn get_resources_to_charge_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    base_pubdata: Option<u64>,
) -> Result<(u64, S::Resources), SystemError> {
    let current_pubdata_spent = system
        .net_pubdata_used()?
        .saturating_sub(base_pubdata.unwrap_or(0));
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
}
```
