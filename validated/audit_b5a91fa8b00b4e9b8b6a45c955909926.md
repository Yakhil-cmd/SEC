### Title
Zero-Length L2→L1 Message Accepted Without Validation Enables Log-Slot Exhaustion Griefing — (`system_hooks/src/call_hooks/l1_messenger.rs`)

### Summary

`send_to_l1_inner` accepts zero-length message payloads without any validation. Because the L2→L1 log Merkle tree is fixed at 16,384 slots per block, an unprivileged user can cheaply exhaust all available log slots within a block by repeatedly sending zero-length messages through the L1 Messenger system contract, preventing legitimate L2→L1 messages from being included.

### Finding Description

The `send_to_l1_inner` function in `system_hooks/src/call_hooks/l1_messenger.rs` validates only that the calldata is at least 20 bytes long (to extract the sender address), but performs no check on the length of the message portion:

```rust
// system_hooks/src/call_hooks/l1_messenger.rs, lines 141-161
if calldata.len() < 20 {
    return Ok(Err(
        "L1 messenger failure: sendToL1 called with invalid calldata",
    ));
}
let address_sender = B160::try_from_be_slice(&calldata[0..20])...;
let message = &calldata[20..];   // ← can be empty slice, no check
system.io.emit_l1_message(
    ExecutionEnvironmentType::NoEE,
    resources,
    &address_sender,
    message,   // ← zero-length accepted
)?;
``` [1](#0-0) 

`emit_l1_message` in `basic_system/src/system_implementation/system/io_subsystem.rs` charges native resources proportional to `data.len()`, so for a zero-length message the per-slot cost is minimized to only the fixed hashing and storage base costs:

```rust
let native = hashing_native_cost
    + EVENT_STORAGE_BASE_NATIVE_COST
    + EVENT_DATA_PER_BYTE_COST * (data.len() as u64);  // 0 for empty message
``` [2](#0-1) 

`push_message` in `zk_ee/src/common_structs/logs_storage.rs` unconditionally appends the entry to the log list with no capacity pre-check:

```rust
pub fn push_message(...) -> Result<(), SystemError> {
    let total_pubdata = 4 + data.len() + L2_TO_L1_LOG_SERIALIZE_SIZE;
    ...
    self.list.push(LogContent { ... }, total_pubdata);
    Ok(())
}
``` [3](#0-2) 

The block-level guard in `basic_bootloader/src/bootloader/block_flow/zk/mod.rs` only invalidates the *transaction that crosses the limit*, not the prior spam transactions that already consumed slots:

```rust
} else if !cfg!(feature = "resources_for_tester") && logs_used > MAX_NUMBER_OF_LOGS {
    Err(InvalidTransaction::BlockL2ToL1LogsLimitReached)
``` [4](#0-3) 

The fixed tree capacity is 16,384 slots:

```rust
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
``` [5](#0-4) 

Each zero-length message consumes exactly 92 bytes of pubdata (`4 + 0 + 88`) and one log slot, which is the minimum possible cost per slot. A non-empty message costs more pubdata per slot. This asymmetry makes zero-length messages the cheapest griefing vector.

### Impact Explanation

A griefer can send many transactions, each calling the L1 Messenger system contract (`0x8008`) with `sendToL1("")` (empty bytes). Each call stores one zero-length entry in the `LogsStorage`, consuming one of the 16,384 available log slots for the block. Once all slots are consumed, any subsequent transaction that attempts to emit an L2→L1 message is invalidated with `BlockL2ToL1LogsLimitReached`. Legitimate bridge withdrawals, cross-chain messages, and protocol-level L1 notifications are blocked for the remainder of the block. The `apply_to_array_vec` function, which pushes log hashes into a fixed-size `ArrayVec<Bytes32, 16384>`, would also panic if the list somehow exceeded 16,384 entries before the block-level check fires. [6](#0-5) 

### Likelihood Explanation

The attack path is fully unprivileged: any user can call the L1 Messenger system contract at `0x8008` with an empty message payload. The Rust hook enforces only that the caller is `0x8008`; it explicitly defers content validation to the system contract with the comment "the L1 messenger system contract should guarantee correct usage," but no such guarantee is enforced in the production Rust path. The cost is bounded by EVM gas and pubdata, but zero-length messages minimize both, making this the cheapest possible way to exhaust log slots. A motivated griefer targeting a specific block (e.g., to delay a bridge withdrawal) has a clear, low-cost path. [7](#0-6) 

### Recommendation

Add a zero-length check in `send_to_l1_inner` before calling `emit_l1_message`:

```rust
let message = &calldata[20..];
if message.is_empty() {
    return Ok(Err("L1 messenger failure: empty message not allowed"));
}
```

Additionally, add a capacity pre-check in `push_message` in `logs_storage.rs` to return an error (rather than silently overflow) if `self.list.len() as u64 >= MAX_NUMBER_OF_LOGS`.

### Proof of Concept

1. Deploy a contract that calls `address(0x8008).call(abi.encodeWithSignature("sendToL1(bytes)", ""))` in a loop.
2. Submit transactions calling this contract until `logs_used` approaches 16,384.
3. Each zero-length message is accepted by `send_to_l1_inner` (calldata = 20-byte address + 0 bytes), stored by `push_message`, and counted toward the block log limit.
4. Once the limit is reached, any subsequent transaction attempting to emit an L2→L1 message is invalidated with `BlockL2ToL1LogsLimitReached`, blocking legitimate bridge withdrawals for the block.

### Citations

**File:** system_hooks/src/call_hooks/l1_messenger.rs (L44-55)
```rust
    // Can be used only by L1 messenger system contract
    if caller != L1_MESSENGER_ADDRESS {
        system_log!(
            system,
            "L1 messenger hook: invalid caller (caller={caller:?})\n"
        );
        // Pretend to be an empty account
        return Ok((
            make_return_state_from_returndata_region(available_resources, &[]),
            return_memory,
        ));
    }
```

**File:** system_hooks/src/call_hooks/l1_messenger.rs (L141-161)
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
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L210-214)
```rust
        let native = hashing_native_cost
            + EVENT_STORAGE_BASE_NATIVE_COST
            + EVENT_DATA_PER_BYTE_COST * (data.len() as u64);

        resources.charge(&R::from_native(R::Native::from_computational(native)))?;
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L25-25)
```rust
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L181-211)
```rust
    pub fn push_message(
        &mut self,
        tx_number: u32,
        address: &B160,
        data: UsizeAlignedByteBox<A>,
        data_hash: Bytes32,
    ) -> Result<(), SystemError> {
        // We are publishing message data(4 bytes to encode length) and underlying log
        // TODO: double check that we should have 4 here
        let total_pubdata = 4 + data.len() + L2_TO_L1_LOG_SERIALIZE_SIZE;
        let total_pubdata = total_pubdata as u32;

        let total_pubdata = self
            .list
            .top()
            .map_or(total_pubdata, |(_, m)| *m + total_pubdata);

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
    }
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L311-315)
```rust
    pub fn apply_to_array_vec(&self, array_vec: &mut ArrayVec<Bytes32, 16384>) {
        self.list.iter().for_each(|el| {
            let log: L2ToL1Log = el.into();
            array_vec.push(log.hash())
        });
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L84-90)
```rust
    } else if !cfg!(feature = "resources_for_tester") && logs_used > MAX_NUMBER_OF_LOGS {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block logs limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockL2ToL1LogsLimitReached)
```
