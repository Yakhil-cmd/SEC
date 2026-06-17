### Title
Silent `u32`→`u16` Truncation of `tx_number_in_block` Causes Log Identifier Collision for Blocks with >65535 Transactions - (File: `zk_ee/src/common_structs/logs_storage.rs`)

---

### Summary

The `L2ToL1Log` struct uses a `u16` field `tx_number_in_block` to identify which transaction produced a given L2→L1 log. Internally, the transaction counter is tracked as `u32`. The conversion silently truncates the `u32` to `u16` at the point of log construction. If a block contains more than 65,535 successful transactions, two distinct transactions will share the same `tx_number_in_block` value in their emitted logs, producing identifier collisions in the L2→L1 log Merkle tree that is committed to L1 as part of the batch public input.

---

### Finding Description

The internal log storage tracks each log's originating transaction as `tx_number: u32` inside `GenericLogContent`: [1](#0-0) 

When a `LogContent` is converted to the wire-format `L2ToL1Log` (used for Merkle tree construction, pubdata serialization, and hashing), the conversion performs an unchecked cast: [2](#0-1) 

The `tx_number_in_block` field is `u16`, capped at 65,535. The block-level transaction counter is `u32` with no enforced upper bound tied to this field width: [3](#0-2) 

The counter is incremented unconditionally for every successfully committed transaction: [4](#0-3) 

The `tx_number_in_block` field is serialized into the 88-byte log encoding that feeds the Merkle tree and pubdata: [5](#0-4) 

The Merkle tree root over these log hashes is included in `BatchOutput`, which is hashed into the ZK public input committed to L1: [6](#0-5) 

---

### Impact Explanation

For any block where `current_transaction_number` exceeds 65,535, the `as u16` cast wraps silently: transaction 65,536 gets `tx_number_in_block = 0`, colliding with transaction 0. Concretely:

1. **Incorrect log hashes** — the `tx_number_in_block` field is part of the 88-byte leaf encoding that is keccak-hashed into the Merkle tree. A wrong value produces a wrong leaf hash.
2. **Wrong Merkle tree root** — the `l2_logs_tree_root` committed in `BatchOutput` is computed from these wrong leaf hashes.
3. **Ambiguous message-inclusion proofs on L1** — a user proving a message from transaction 65,536 must supply `tx_number_in_block = 0` (the truncated value) to match the committed root. This is indistinguishable from a proof for a log from transaction 0 with the same `key`/`value`, enabling cross-transaction log attribution confusion.
4. **Pubdata integrity** — the same wrong `tx_number_in_block` is written into pubdata used for state recovery. [7](#0-6) 

---

### Likelihood Explanation

The minimum EVM intrinsic gas per transaction is 21,000. Reaching 65,536 transactions requires at least ~1.37 billion gas in a single block. Current ZKsync block gas limits make this practically unreachable under normal operation. However:

- No explicit per-block transaction count cap is enforced in `check_for_block_limits` (which only checks gas, native resources, pubdata bytes, and log count).
- A future configuration with a very high block gas limit, or a chain using service/upgrade transactions with near-zero gas cost, could approach this threshold.
- The `MAX_NUMBER_OF_LOGS` cap of 16,384 limits logs but not transactions. [8](#0-7) 

Likelihood is **low** under current parameters but non-zero for high-throughput or specially configured deployments.

---

### Recommendation

Replace the silent truncation with a checked conversion that either:
- Panics/returns an error if `tx_number > u16::MAX`, or
- Widens `tx_number_in_block` in `L2ToL1Log` to `u32` (matching the internal counter width) and updates the 88-byte serialization format accordingly.

At minimum, add an assertion at the cast site:

```rust
// In logs_storage.rs, From<&LogContent<A>> for L2ToL1Log
tx_number_in_block: u16::try_from(m.tx_number)
    .expect("tx_number exceeds u16::MAX; log identifier collision"),
```

Additionally, enforce a hard cap on `current_transaction_number` inside `check_for_block_limits` to guarantee the invariant at the block level.

---

### Proof of Concept

1. Construct a block containing 65,537 transactions, each emitting one L2→L1 message (e.g., via the L1 messenger hook at `0x7001`).
2. Transaction 0 emits message `M0`; transaction 65,536 emits message `M65536`.
3. After block execution, inspect the serialized `L2ToL1Log` for transaction 65,536: `tx_number_in_block` will be `0` (truncated from `65536`), identical to the log for transaction 0.
4. Both logs produce different leaf hashes only because their `key`/`value` differ. If an attacker crafts transaction 65,536 to emit the same `key`/`value` as transaction 0, the two Merkle leaves are identical — a complete identifier collision allowing either log to satisfy the other's inclusion proof on L1. [9](#0-8)

### Citations

**File:** zk_ee/src/common_structs/logs_storage.rs (L23-25)
```rust
pub const L2_TO_L1_LOG_SERIALIZE_SIZE: usize = 88;
// Taken from the size of the Merkle tree.
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L72-75)
```rust
pub struct GenericLogContent<IOTypes: SystemIOTypesConfig, A: Allocator = Global> {
    pub tx_number: u32,
    pub data: GenericLogContentData<UsizeAlignedByteBox<A>, Bytes32, IOTypes::Address>,
}
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L278-309)
```rust
    pub fn apply_pubdata<T: WriteBytes + ?Sized>(
        &self,
        dst: &mut T,
        results_keeper: &mut impl IOResultKeeper<EthereumIOTypesConfig>,
    ) {
        let logs_count = (self.list.len() as u32).to_be_bytes();
        dst.write(&logs_count);
        results_keeper.pubdata(&logs_count);
        let mut messages_count: u32 = 0;
        // First we encode all the L2L1 log information.
        self.list.iter().for_each(|el| {
            if let GenericLogContentData::UserMsg(_) = el.data {
                messages_count += 1;
            }
            let log: L2ToL1Log = el.into();
            log.write_encoding(dst);
            log.pubdata(results_keeper);
        });
        // Then, we do a second pass to publish messages
        let messages_count = messages_count.to_be_bytes();
        dst.write(&messages_count);
        results_keeper.pubdata(&messages_count);
        self.list.iter().for_each(|el| {
            if let GenericLogContentData::UserMsg(UserMsgData { data, .. }) = &el.data {
                let len = (data.as_slice().len() as u32).to_be_bytes();
                dst.write(&len);
                results_keeper.pubdata(&len);
                dst.write(data.as_slice());
                results_keeper.pubdata(data.as_slice());
            }
        })
    }
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L449-458)
```rust
    pub fn encode(&self) -> [u8; L2_TO_L1_LOG_SERIALIZE_SIZE] {
        let mut buffer = [0u8; L2_TO_L1_LOG_SERIALIZE_SIZE];
        buffer[0..1].copy_from_slice(&[self.l2_shard_id]);
        buffer[1..2].copy_from_slice(&[if self.is_service { 1 } else { 0 }]);
        buffer[2..4].copy_from_slice(&self.tx_number_in_block.to_be_bytes());
        buffer[4..24].copy_from_slice(&self.sender.to_be_bytes::<20>());
        buffer[24..56].copy_from_slice(self.key.as_u8_ref());
        buffer[56..88].copy_from_slice(self.value.as_u8_ref());
        buffer
    }
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L495-524)
```rust
impl<A: Allocator> From<&LogContent<A>> for L2ToL1Log {
    fn from(m: &LogContent<A>) -> Self {
        let (sender, key, value) = match m.data {
            GenericLogContentData::UserMsg(UserMsgData {
                address, data_hash, ..
            }) => (
                // TODO: move into const
                B160::from_limbs([0x8008, 0, 0]),
                address.into(),
                data_hash,
            ),
            GenericLogContentData::L1TxLog(L1TxLog { tx_hash, success }) => {
                let data = if success { U256::from(1) } else { U256::ZERO };
                (
                    // TODO: move into const
                    B160::from_limbs([0x8001, 0, 0]),
                    tx_hash,
                    Bytes32::from_u256_be(&data),
                )
            }
        };
        Self {
            l2_shard_id: 0,
            is_service: true,
            tx_number_in_block: m.tx_number as u16,
            sender,
            key,
            value,
        }
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/block_data.rs (L6-9)
```rust
pub struct ZKBasicBlockDataKeeper<EA: TxHashesAccumulator> {
    /// Current transaction number within the block
    pub current_transaction_number: u32,
    /// Rolling Keccak hash of all transaction hashes in execution order
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L212-212)
```rust
                                block_data.current_transaction_number += 1;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L68-75)
```rust
    pub priority_operations_hash: Bytes32,
    /// L2 logs tree root.
    /// Note that it's full root, it's keccak256 of:
    /// - merkle root of l2 -> l1 logs in the batch .
    /// - multichain root - commitment to logs emitted on chains that settle on the current.
    pub l2_logs_tree_root: Bytes32,
    /// Protocol upgrade tx hash (0 if there wasn't)
    pub upgrade_tx_hash: Bytes32,
```
