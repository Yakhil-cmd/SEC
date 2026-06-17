### Title
`pubdata_price` and `native_price` Omitted from Block Header Commitment, Enabling Prover-Controlled Fee Manipulation — (`basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs`)

---

### Summary

The ZKsync-specific pricing parameters `pubdata_price` and `native_price`, read from the oracle at block start via `BlockMetadataFromOracle`, are used throughout transaction fee validation and resource accounting but are **never committed to the block header or ZK proof public inputs**. This is explicitly acknowledged in the codebase with a `// TODO: add pubdata price and native price` comment and in the bootloader documentation. A prover (an explicitly in-scope attacker entry path) can supply different values for these fields during proving execution than the sequencer used during forward execution, producing a valid ZK proof for a state transition with manipulated fee accounting.

---

### Finding Description

`BlockMetadataFromOracle` carries two ZKsync-specific pricing fields:

```rust
pub pubdata_price: U256,
pub native_price: U256,
``` [1](#0-0) 

These are consumed directly in ZK transaction validation to compute `native_per_gas` and `native_per_pubdata`, which gate whether a transaction is accepted and how much it is charged:

```rust
let pubdata_price = system.get_pubdata_price();
let native_price = system.get_native_price();
// ...
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

At block finalization, `form_block_header` constructs the `BlockHeader` that is hashed and committed to the ZK proof public inputs. It explicitly skips these two fields:

```rust
let base_fee_per_gas = system.get_eip1559_basefee();
// TODO: add pubdata price and native price
``` [3](#0-2) 

The `BlockHeader` struct itself has no fields for `pubdata_price` or `native_price`: [4](#0-3) 

The bootloader documentation explicitly acknowledges this gap:

> "The block header should determine the block fully, i.e. include all the inputs needed to execute the block. Currently it misses `gas_per_pubdata` and `native_price`, but we already working on design and implementation to solve this issue." [5](#0-4) 

The public input structure (`blocks_output_hash`, `chain_state_commitment`) does not include these values either: [6](#0-5) 

---

### Impact Explanation

Because `pubdata_price` and `native_price` are not committed to the ZK proof public inputs, the prover can supply values during proving execution that differ from those used by the sequencer during forward execution. The L1 verifier has no way to detect this discrepancy.

Concrete consequences:

1. **Forward/proving divergence in fee accounting**: The proven state transition (with manipulated `pubdata_price`/`native_price`) differs from the sequencer-executed state transition. The L1 verifier accepts the proven state, not the executed state.
2. **User fund manipulation**: A prover using a higher `pubdata_price` during proving charges users more for pubdata than they were told during forward execution — a direct loss of funds.
3. **Transaction outcome manipulation**: Setting `native_price` to an extreme value can flip transaction outcomes (pass → fail or fail → pass) between forward and proving execution, corrupting the canonical chain state.

---

### Likelihood Explanation

The prover is an explicitly listed attacker-controlled entry path in the Immunefi scope ("prover/forward execution input"). The prover supplies oracle data via CSR writes in the RISC-V proving environment (`CsrBasedIOOracle`). Since `pubdata_price` and `native_price` are read from the oracle and not constrained by the ZK proof, the prover can freely choose these values. The attack requires no special key or governance access — only the ability to run the prover, which is the prover's normal role. The codebase itself documents the missing commitment as a known gap.

---

### Recommendation

Include `pubdata_price` and `native_price` in the committed block header before the ZK proof is finalized. The documentation already identifies `extra_data` as the intended location: [7](#0-6) 

Concretely, `form_block_header` should encode both values into `extra_data` (or a dedicated header field), and the `BlockHeader` RLP encoding used to compute `computed_header_hash` must include them so they become part of the public input.

---

### Proof of Concept

1. Sequencer runs forward execution with `BlockMetadataFromOracle { pubdata_price: P, native_price: N }`. Transaction T is accepted and user is charged fee F.
2. Prover runs proving execution with `BlockMetadataFromOracle { pubdata_price: P * 10, native_price: N }`. Transaction T is charged fee `F * 10`, draining more from the user's balance.
3. `form_block_header` produces a `BlockHeader` containing only `base_fee_per_gas`; `pubdata_price` and `native_price` are absent.
4. The ZK proof is generated over the proving execution state (with fee `F * 10`). The proof is valid.
5. The L1 verifier checks the proof against the public input hash, which does not include `pubdata_price` or `native_price`. The proof passes.
6. The on-chain state reflects the manipulated fee `F * 10`, not the sequencer's `F`. Users lose funds with no on-chain evidence of manipulation. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs (L68-100)
```rust
/// Helper method to create block header.
fn form_block_header<S: EthereumLikeTypes>(
    system: &System<S>,
    tx_rolling_hash: Bytes32,
    block_gas_used: u64,
) -> Result<BlockHeader, BootloaderSubsystemError> {
    let block_number = system.get_block_number();
    let previous_block_hash = if block_number == 0 {
        Bytes32::ZERO
    } else {
        system.get_blockhash(block_number - 1)?
    };
    let beneficiary = system.get_coinbase();
    let gas_limit = system.get_gas_limit();
    let timestamp = system.get_timestamp();
    let consensus_random = system.get_mix_hash()?;
    let base_fee_per_gas = system.get_eip1559_basefee();
    // TODO: add pubdata price and native price
    let base_fee_per_gas = base_fee_per_gas
        .try_into()
        .map_err(|_| internal_error!("base_fee_per_gas exceeds max u64"))?;

    Ok(BlockHeader::new(
        previous_block_hash,
        beneficiary,
        tx_rolling_hash,
        block_number,
        gas_limit,
        block_gas_used,
        timestamp,
        consensus_random,
        base_fee_per_gas,
    ))
```

**File:** basic_bootloader/src/bootloader/block_header.rs (L22-75)
```rust
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BlockHeader {
    /// The Keccak 256-bit hash of the parent
    /// block’s header, in its entirety; formally Hp.
    pub parent_hash: Bytes32,
    /// The Keccak 256-bit hash of the ommers list portion of this block; formally Ho.
    pub ommers_hash: Bytes32,
    /// The 160-bit address to which all fees collected from the successful mining of this block
    /// be transferred; formally Hc.
    pub beneficiary: B160,
    /// The Keccak 256-bit hash of the root node of the state trie, after all transactions are
    /// executed and finalisations applied; formally Hr.
    pub state_root: Bytes32,
    /// The Keccak 256-bit hash of the root node of the trie structure populated with each
    /// transaction in the transactions list portion of the block; formally Ht.
    pub transactions_root: Bytes32,
    /// The Keccak 256-bit hash of the root node of the trie structure populated with the receipts
    /// of each transaction in the transactions list portion of the block; formally He.
    pub receipts_root: Bytes32,
    /// The Bloom filter composed from indexable information (logger address and log topics)
    /// contained in each log entry from the receipt of each transaction in the transactions list;
    /// formally Hb.
    pub logs_bloom: [u8; 256],
    /// A scalar value corresponding to the difficulty level of this block. This can be calculated
    /// from the previous block’s difficulty level and the timestamp; formally Hd.
    pub difficulty: U256,
    /// A scalar value equal to the number of ancestor blocks. The genesis block has a number of
    /// zero; formally Hi.
    pub number: u64,
    /// A scalar value equal to the current limit of gas expenditure per block; formally Hl.
    pub gas_limit: u64,
    /// A scalar value equal to the total gas used in transactions in this block; formally Hg.
    pub gas_used: u64,
    /// A scalar value equal to the reasonable output of Unix’s time() at this block’s inception;
    /// formally Hs.
    pub timestamp: u64,
    /// An arbitrary byte array containing data relevant to this block. This must be 32 bytes or
    /// fewer; formally Hx.
    pub extra_data: ArrayVec<u8, 32>,
    /// A 256-bit hash which, combined with the
    /// nonce, proves that a sufficient amount of computation has been carried out on this block;
    /// formally Hm.
    pub mix_hash: Bytes32,
    /// A 64-bit value which, combined with the mixhash, proves that a sufficient amount of
    /// computation has been carried out on this block; formally Hn.
    pub nonce: [u8; 8],
    /// A scalar representing EIP1559 base fee which can move up or down each block according
    /// to a formula which is a function of gas used in parent block and gas target
    /// (block gas limit divided by elasticity multiplier) of parent block.
    /// The algorithm results in the base fee per gas increasing when blocks are
    /// above the gas target, and decreasing when blocks are below the gas target. The base fee per
    /// gas is burned.
    pub base_fee_per_gas: u64,
}
```

**File:** docs/bootloader/bootloader.md (L35-36)
```markdown
The block header should determine the block fully, i.e. include all the inputs needed to execute the block.
Currently it misses `gas_per_pubdata` and `native_price`, but we already working on design and implementation to solve this issue.
```

**File:** docs/bootloader/bootloader.md (L52-52)
```markdown
| extra_data          | any extra data included by proposer                                              | TBD, possibly gas_per_pubdata and native price                     |                                         |
```

**File:** docs/l1_integration.md (L37-58)
```markdown
Block(s) public input will be computed as `blake2s` hash of the following values(concatenated):
- `chain_state_commitment_before`
- `chain_state_commitment_after`
- `blocks_output_hash`

Where
- `chain_state_commitment_before` is `blake2s` hash of(concatenation):
  - `state_root`
  - `next_free_slot`
  - `block_number`
  - `last_256_block_hashes_blake`
  - `last_block_timestamp`
    before the block(s).
- `chain_state_commitment_after` same hash, but of values after the block(s).
- `blocks_output_hash` is `blake2s` hash of(concatenation):
  - `used_chain_id`
  - `first_block_timestamp`
  - `last_block_timestamp` (equals to `first_block_timestamp` for a single block)
  - `pubdata_blake2s_hash`
  - `priority_ops_hashes_blakes2s_hash` (l1 txs)
  - `l2_to_l1_logs_hashes_blake2s_hash`
  - `upgrade_tx_hash`
```
