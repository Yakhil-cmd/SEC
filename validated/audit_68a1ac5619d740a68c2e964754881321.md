### Title
Oracle-Provided Block Metadata Fields Not Committed to Public Input Allow Prover to Manipulate Execution Parameters — (`zk_ee/src/system/metadata/zk_metadata.rs`, `basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs`)

---

### Summary

`BlockMetadataFromOracle` contains multiple execution-critical fields that are read from the oracle and used during block execution but are **never included in the public input commitment**. A malicious prover can supply different values for these fields in the proving run than what was used in the forward run, causing the settlement layer to accept a state transition that was computed under manipulated block parameters.

---

### Finding Description

`BlockMetadataFromOracle` is deserialized from the oracle at the start of every block:

```rust
// basic_bootloader/src/bootloader/block_flow/zk/metadata_op.rs:18-19
let block_level_metadata: BlockMetadataFromOracle =
    oracle.query_with_empty_input(BLOCK_METADATA_QUERY_ID)?;
```

The struct contains twelve fields:

```rust
// zk_ee/src/system/metadata/zk_metadata.rs:114-132
pub struct BlockMetadataFromOracle {
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
    pub mix_hash: U256,
    pub blob_fee: U256,
}
```

The public input is assembled from two structures. `ChainStateCommitment` commits to `state_root`, `next_free_slot`, `block_number`, `last_256_block_hashes_blake` (derived from `block_hashes`), and `last_block_timestamp` (derived from `timestamp`):

```rust
// basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs:33-41
pub fn hash(&self) -> [u8; 32] {
    hasher.update(self.state_root.as_u8_ref());
    hasher.update(&self.next_free_slot.to_be_bytes());
    hasher.update(&self.block_number.to_be_bytes());
    hasher.update(self.last_256_block_hashes_blake.as_u8_ref());
    hasher.update(&self.last_block_timestamp.to_be_bytes());
    ...
}
```

`BatchOutput` commits to `chain_id`, timestamps, `da_commitment_scheme`, `pubdata_commitment`, tx counts, `priority_operations_hash`, `l2_logs_tree_root`, `upgrade_tx_hash`, `interop_roots_rolling_hash`, and `settlement_layer_chain_id`:

```rust
// basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs:86-103
pub fn hash(&self) -> [u8; 32] {
    hasher.update(self.chain_id.to_be_bytes::<32>());
    hasher.update(&self.first_block_timestamp.to_be_bytes());
    hasher.update(&self.last_block_timestamp.to_be_bytes());
    hasher.update([self.da_commitment_scheme as u8]);
    hasher.update(self.pubdata_commitment.as_u8_ref());
    ...
}
```

The following fields from `BlockMetadataFromOracle` are **absent from both commitment structures** and therefore absent from the final `BatchPublicInput` hash:

| Field | EVM Effect |
|---|---|
| `eip1559_basefee` | `BASEFEE` opcode; fee charging |
| `coinbase` | `COINBASE` opcode; fee recipient |
| `mix_hash` | `PREVRANDAO` opcode; on-chain randomness |
| `pubdata_price` | pubdata cost per byte |
| `native_price` | native token pricing |
| `gas_limit` | block gas capacity |
| `pubdata_limit` | pubdata capacity |
| `blob_fee` | blob base fee |

The oracle documentation itself acknowledges the gap: "Reading block metadata (this is verified by having it as part of the public inputs)" — but only `chain_id`, `block_number`, `timestamp`, and `block_hashes` are actually committed to. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

A malicious prover can run the forward execution with correct block parameters, then re-run the proving execution with manipulated parameters. Because the settlement layer only verifies the public input hash (which does not cover these fields), it cannot detect the divergence.

Concrete impacts:

1. **`mix_hash` (PREVRANDAO) manipulation**: The prover can set `mix_hash` to any chosen value. Contracts that use `PREVRANDAO` for randomness (lotteries, NFT minting, commit-reveal schemes) can be made to produce a prover-chosen outcome. The prover can scan the forward run, identify a favorable `mix_hash`, and re-prove with that value.

2. **`eip1559_basefee` manipulation**: Setting `basefee = 0` allows transactions that would fail `BASEFEE` checks to succeed, or allows the prover to bypass EIP-1559 fee enforcement entirely.

3. **`coinbase` redirection**: The prover can redirect all block fee revenue to an arbitrary address without the settlement layer detecting the change.

4. **`pubdata_price` / `pubdata_limit` manipulation**: Lowering `pubdata_price` to zero allows the prover to include transactions that would otherwise be rejected for exceeding pubdata cost limits, enabling state transitions that users could not have legitimately triggered.

The state root after the manipulated execution is different from the honest execution, and the settlement layer accepts it as valid because the ZK proof only proves internal consistency of the RISC-V execution, not that the oracle inputs matched the forward run. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

The attacker is the prover/sequencer, which is explicitly listed as an in-scope attacker role ("prover/forward execution input"). No external dependency, leaked key, or governance majority is required. The prover already controls oracle data delivery to the RISC-V program. Exploiting `mix_hash` requires only: (1) observing the forward run to identify a target contract using `PREVRANDAO`, (2) brute-forcing or choosing a favorable `mix_hash` value, and (3) re-running the proving execution with that value. Steps 1 and 3 are already part of normal prover operation. [6](#0-5) [7](#0-6) 

---

### Recommendation

Include all execution-affecting fields of `BlockMetadataFromOracle` in the public input commitment. At minimum, add `eip1559_basefee`, `coinbase`, `mix_hash`, `pubdata_price`, `native_price`, `gas_limit`, `pubdata_limit`, and `blob_fee` to `BatchOutput` (or a new `BlockParams` sub-commitment hashed into `BatchOutput`):

```rust
pub struct BatchOutput {
    // existing fields ...
    pub eip1559_basefee: U256,
    pub coinbase: B160,
    pub mix_hash: U256,
    pub pubdata_price: U256,
    pub native_price: U256,
    pub gas_limit: u64,
    pub pubdata_limit: u64,
    pub blob_fee: U256,
}
```

This mirrors the fix applied to BaseVault: include the previously uncommitted data in the leaf/commitment so that the privileged party cannot freely choose it. [8](#0-7) 

---

### Proof of Concept

1. Deploy a contract on ZKsync OS that reads `PREVRANDAO` and pays out a jackpot if `prevrandao % 1000 == 0`.
2. Prover runs the forward execution with the honest `mix_hash` value (e.g., `0xABCD...`). The contract does not pay out.
3. Prover iterates over candidate `mix_hash` values until finding one where `mix_hash % 1000 == 0`.
4. Prover re-runs the RISC-V proving execution with `mix_hash = <chosen value>` supplied via the oracle. The contract pays out to the prover's address.
5. The resulting `state_after` (with the jackpot transferred) is different from the honest `state_after`.
6. The prover submits the proof. The settlement layer verifies the ZK proof and the public input hash. Since `mix_hash` is not in the public input, the proof is valid and the manipulated `state_after` is accepted.

The entry path is: oracle CSR write in the RISC-V proving environment → `BLOCK_METADATA_QUERY_ID` response → `BlockMetadataFromOracle::from_iter` → `mix_hash` field → EVM `PREVRANDAO` opcode → contract payout. [9](#0-8) [10](#0-9)

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

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L155-160)
```rust
        self.timestamp
    }

    fn block_randomness(&self) -> Option<Bytes32> {
        Some(Bytes32::from_array(self.mix_hash.to_be_bytes::<32>()))
    }
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L26-103)
```rust
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
}

///
/// Except for proving existence of batch(of blocks) that changes state from one to another, we want to open some info about this batch on the settlement layer:
/// - pubdata: to make sure that it's published and state is recoverable
/// - executed priority ops: to process them on the settlement layer
/// - l2 to l1 logs tree root: to be able to open them on the settlement layer
/// - extra inputs to validate on the settlement layer(timestamp and chain id)
///
#[derive(Debug)]
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs (L185-205)
```rust
        let batch_output = BatchOutput {
            chain_id: U256::from(metadata.chain_id()),
            first_block_timestamp: metadata.block_timestamp(),
            last_block_timestamp: metadata.block_timestamp(),
            da_commitment_scheme: io.da_commitment_scheme.unwrap(),
            pubdata_commitment: da_commitment,
            number_of_layer_1_txs: U256::try_from(number_of_layer_1_txs).unwrap(),
            number_of_layer_2_txs: U256::from(number_of_layer_2_txs),
            priority_operations_hash,
            l2_logs_tree_root: full_l2_to_l1_logs_root,
            upgrade_tx_hash,
            interop_roots_rolling_hash,
            settlement_layer_chain_id,
        };
        logger_log!(logger, "PI calculation: batch output {:?}\n", batch_output,);

        let public_input = BatchPublicInput {
            state_before: chain_state_commitment_before.hash().into(),
            state_after: chain_state_commitment_after.hash().into(),
            batch_output: batch_output.hash().into(),
        };
```

**File:** docs/system/io/oracles.md (L10-12)
```markdown
- Reading the next transaction size and data.
- Reading block metadata (this is verified by having it as part of the public inputs).
- Retrieving preimages for bytecode and account hashes. Bytecode hashes are verified (recomputed) before actually using the bytecode, while account hashes are verified while materializing the account properties for the first time (both are only done if running in proving environment).
```

**File:** zk_ee/src/oracle/mod.rs (L13-16)
```rust
//! # Security Model
//!
//! **Critical**: Oracle responses are treated as **untrusted input**. The oracle system does not validate data authenticity or correctness. All oracle
//! responses MUST be validated by the calling code before use.
```

**File:** zk_ee/src/common_structs/da_commitment_scheme.rs (L44-50)
```rust
impl DACommitmentScheme {
    pub fn try_from_oracle<O: IOOracle>(oracle: &mut O) -> Result<Self, InternalError> {
        let da_commitment_scheme_id_raw: u8 =
            oracle.query_with_empty_input(DA_COMMITMENT_SCHEME_QUERY_ID)?;
        DACommitmentScheme::try_from(da_commitment_scheme_id_raw)
            .map_err(|_| internal_error!("Invalid DA commitment scheme ID"))
    }
```
