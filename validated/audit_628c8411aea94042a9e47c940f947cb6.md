### Title
Griefing via Zero-Byte L2→L1 Messages Exhausts Block Pubdata Budget and Log Merkle Tree Slots — (`system_hooks/src/call_hooks/l1_messenger.rs`)

### Summary

The L1 Messenger system hook (`send_to_l1_inner`) imposes no minimum message size or value. Any unprivileged user can call the L1 Messenger system contract (`0x8008`) with an empty payload, causing zero-byte entries to be pushed into the `LogsStorage`. By flooding a block with such dust messages an attacker exhausts the block's pubdata budget and/or the fixed 16 384-slot L2→L1 log Merkle tree, preventing legitimate users from including their own L2→L1 messages (e.g., withdrawal proofs) in the same block.

---

### Finding Description

`send_to_l1_inner` in `system_hooks/src/call_hooks/l1_messenger.rs` decodes the caller-supplied calldata as `abi.encodePacked(address sender, bytes message)`. The only structural guard is:

```rust
if calldata.len() < 20 {
    return Ok(Err("L1 messenger failure: sendToL1 called with invalid calldata"));
}
let message = &calldata[20..];   // message can be 0 bytes
system.io.emit_l1_message(ExecutionEnvironmentType::NoEE, resources, &address_sender, message)?;
``` [1](#0-0) 

There is no check that `message` is non-empty or meets any minimum size. `emit_l1_message` in the IO subsystem charges native resources proportional to `data.len()`, but for a zero-byte message the incremental cost is minimal:

```rust
let native = hashing_native_cost
    + EVENT_STORAGE_BASE_NATIVE_COST
    + EVENT_DATA_PER_BYTE_COST * (data.len() as u64);  // 0 for empty message
``` [2](#0-1) 

Each accepted message is pushed unconditionally into `LogsStorage` with no capacity guard at the push site:

```rust
self.list.push(LogContent { tx_number, data: GenericLogContentData::UserMsg(...) }, total_pubdata);
Ok(())
``` [3](#0-2) 

The Merkle tree used for L2→L1 log commitments is fixed at **16 384 leaves** (`MAX_NUMBER_OF_LOGS = 16_384`). `tree_root()` allocates a `Vec` of size `self.list.len()` and iterates over every entry: [4](#0-3) [5](#0-4) 

`apply_pubdata` also performs two full passes over the list at block finalization: [6](#0-5) 

Each zero-byte message still contributes `4 (length prefix) + 0 (data) + 88 (L2ToL1Log serialization) = 92 bytes` of pubdata. The block pubdata budget is therefore the binding constraint on how many dust messages fit per block.

---

### Impact Explanation

An attacker who floods a block with zero-byte `sendToL1` calls:

1. **Exhausts the pubdata budget** — each dust message consumes 92 bytes of pubdata. Legitimate L2→L1 messages (e.g., ERC-20 withdrawal proofs, which carry non-trivial payloads) are crowded out of the block.
2. **Fills the 16 384-slot Merkle tree** — once all slots are consumed, no further L2→L1 messages can be included in that block, directly blocking user withdrawals until a future block.
3. **Increases block finalization cost** — `tree_root()` and `apply_pubdata()` iterate over every log entry; a full tree of 16 384 entries maximises this work.

The net effect is a griefing attack on withdrawals: legitimate users are forced to wait for subsequent blocks, and the attacker's cost is bounded only by the block gas limit (measured at ~9 202 gas per message from integration tests). [7](#0-6) 

---

### Likelihood Explanation

- The attack path is fully unprivileged: any EOA can call the L1 Messenger system contract at `0x8008` with `sendToL1("")`.
- The per-message EVM gas cost is low (~9 202 gas, confirmed by `test_l1_messenger_gas_charging`). With a 30 M gas block limit an attacker can inject ~3 258 dust messages per block, consuming ~300 KB of pubdata and ~20 % of the Merkle tree.
- No special role, key, or governance access is required.
- The attack is repeatable every block.

---

### Recommendation

1. **Enforce a minimum message length** in `send_to_l1_inner` (e.g., reject messages shorter than 1 byte, or a protocol-defined minimum).
2. **Enforce `MAX_NUMBER_OF_LOGS`** at the `push_message` call site in `LogsStorage`, returning a `SystemError` when the cap is reached so the transaction reverts rather than silently succeeding.
3. **Consider a minimum pubdata fee** for L2→L1 messages, analogous to the `gas_per_pubdata` mechanism used for L1→L2 transactions, to make bulk spam economically unattractive.

---

### Proof of Concept

1. Deploy a simple contract that calls `L1Messenger(0x8008).sendToL1("")` in a loop.
2. Submit a transaction that calls this contract with enough gas to exhaust the block gas limit.
3. Observe that the block's `LogsStorage` is filled with zero-byte entries, the pubdata budget is consumed, and any subsequent `sendToL1` call in the same block reverts (or is excluded by the operator).
4. A legitimate user attempting to send a withdrawal proof in the same block is forced to wait for the next block.

The root cause is the absence of a minimum-size guard in `send_to_l1_inner`: [8](#0-7)

### Citations

**File:** system_hooks/src/call_hooks/l1_messenger.rs (L141-163)
```rust
    if calldata.len() < 20 {
        return Ok(Err(
            "L1 messenger failure: sendToL1 called with invalid calldata",
        ));
    }

    let address_sender = B160::try_from_be_slice(&calldata[0..20]).ok_or(
        SystemError::LeafDefect(internal_error!("Failed to create B160 from 20 byte array")),
    )?;

    let message = &calldata[20..];

    // emit L1 message (ignore returned hash)
    // TODO(EVM-1190): hash calculation is suboptimal, to be refactored in future
    system.io.emit_l1_message(
        // Gas should be charged by the L1Messenger system contract
        ExecutionEnvironmentType::NoEE,
        resources,
        &address_sender,
        message,
    )?;

    Ok(Ok(()))
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L204-214)
```rust
        let hashing_native_cost =
            keccak256_native_cost::<Self::Resources>(L2_TO_L1_LOG_SERIALIZE_SIZE).as_u64()
                + 3 * keccak256_native_cost::<Self::Resources>(64).as_u64()
                + keccak256_native_cost::<Self::Resources>(data.len()).as_u64();

        // We also charge some native resource for storing the log
        let native = hashing_native_cost
            + EVENT_STORAGE_BASE_NATIVE_COST
            + EVENT_DATA_PER_BYTE_COST * (data.len() as u64);

        resources.charge(&R::from_native(R::Native::from_computational(native)))?;
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L23-25)
```rust
pub const L2_TO_L1_LOG_SERIALIZE_SIZE: usize = 88;
// Taken from the size of the Merkle tree.
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L198-210)
```rust
        self.list.push(
            LogContent {
                tx_number,
                data: GenericLogContentData::UserMsg(UserMsgData {
                    address: *address,
                    data,
                    data_hash,
                }),
            },
            total_pubdata,
        );

        Ok(())
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L278-308)
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
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L417-421)
```rust
        let mut elements = alloc::vec::Vec::with_capacity_in(self.list.len(), A::default());
        self.list.iter().for_each(|el| {
            let log: L2ToL1Log = el.into();
            elements.push(log.hash())
        });
```
