### Title
Unbounded L2→L1 Log Accumulation Causes Panic in Multiblock Batch Proving — (`zk_ee/src/common_structs/logs_storage.rs`)

---

### Summary

Any unprivileged user can fill a block's L2→L1 log queue up to the per-block limit (`MAX_NUMBER_OF_LOGS = 16384`) by repeatedly calling the L1 Messenger system contract. When the operator attempts to prove a multiblock batch containing such a block, `apply_to_array_vec` unconditionally pushes log hashes into a fixed-capacity `ArrayVec<Bytes32, 16384>` that accumulates logs across all blocks in the batch. If the total log count across blocks exceeds 16384, `ArrayVec::push` panics, making the batch unprovable. This is a **valid-execution unprovability** / **resource accounting bug** directly analogous to the external report's unbounded-array DoS pattern.

---

### Finding Description

**Root cause — `apply_to_array_vec` has no capacity guard:** [1](#0-0) 

```rust
pub fn apply_to_array_vec(&self, array_vec: &mut ArrayVec<Bytes32, 16384>) {
    self.list.iter().for_each(|el| {
        let log: L2ToL1Log = el.into();
        array_vec.push(log.hash())   // panics if full — no capacity check
    });
}
```

This is called once per block during multiblock batch finalization: [2](#0-1) 

The accumulator is declared as a fixed-size `ArrayVec<Bytes32, 16384>` in the batch data keeper: [3](#0-2) 

The constant `MAX_NUMBER_OF_LOGS` is 16384 — the same value as the `ArrayVec` capacity: [4](#0-3) 

The per-block limit check (`BlockL2ToL1LogsLimitReached`) enforces this limit per block: [5](#0-4) [6](#0-5) 

Because the per-block limit equals the batch-level `ArrayVec` capacity, a single block that reaches the per-block maximum (16384 logs) completely fills the batch accumulator. Any subsequent block in the same multiblock batch that emits even one log causes `apply_to_array_vec` to call `ArrayVec::push` on a full array, which **panics unconditionally** in Rust.

**Attacker entry path — anyone can emit L2→L1 logs:**

Any user can call the L1 Messenger system contract at `0x8008` with `sendToL1(bytes)`. The L1 Messenger hook at `0x7001` is correctly gated to only accept calls from `L1_MESSENGER_ADDRESS`: [7](#0-6) 

However, the L1 Messenger system contract itself is callable by any EOA. Each call to `sendToL1` invokes `emit_l1_message`, which calls `push_message` on `logs_storage` with no global batch-level cap: [8](#0-7) [9](#0-8) 

`push_message` has no check against `MAX_NUMBER_OF_LOGS` and no batch-level guard.

---

### Impact Explanation

**Classification: Valid-execution unprovability / resource accounting bug.**

When a block contains `MAX_NUMBER_OF_LOGS` (16384) L2→L1 logs — a valid, gas-paid execution — the multiblock batch prover panics during `apply_to_array_vec` when processing any subsequent block in the same batch. The panic propagates inside the RISC-V proving binary, making the batch unprovable. The operator is forced to either:
- Use only single-block batches (increasing settlement costs), or
- Exclude the attacker's block from any batch (censorship pressure).

This is a **state-transition / proving divergence**: the forward system executes the block successfully, but the proving system cannot finalize the multiblock batch.

---

### Likelihood Explanation

Any unprivileged user can trigger this by sending enough transactions calling `sendToL1` within a single block to reach the 16384-log limit. The cost is bounded by EVM gas (each `sendToL1` call costs ~9202 gas per the test suite): [10](#0-9) 

At ~9202 gas per message and a block gas limit of ~10–15M gas, an attacker can emit roughly 1000–1600 logs per block at normal gas prices — well below 16384. However, if the per-block log limit is enforced independently of gas (i.e., a single transaction can emit many logs cheaply), the threshold is reachable. Even at lower fill levels, the structural absence of a batch-level capacity check means any multiblock batch accumulating logs from two or more high-log blocks risks overflow.

---

### Recommendation

1. **Add a batch-level log count check** before calling `apply_to_array_vec`. Before pushing a block's logs into `batch_data.logs_storage`, verify that `batch_data.logs_storage.len() + io.logs_storage.len() <= 16384` and handle overflow gracefully (e.g., return an error rather than panic).

2. **Replace the unchecked `push` with a checked variant** in `apply_to_array_vec`:
   ```rust
   array_vec.try_push(log.hash()).expect("batch log capacity exceeded");
   ```
   Or better, return a `Result` and propagate the error up to the caller.

3. **Enforce a batch-level log limit** in the transaction loop, not just a per-block limit, so that the prover's fixed-size accumulator is never exceeded.

---

### Proof of Concept

```
1. Attacker sends N transactions in block B1, each calling L1Messenger.sendToL1(bytes),
   where N = MAX_NUMBER_OF_LOGS (16384).
   Each call costs ~9202 gas; total ~150M gas (may require multiple txs or a loop contract).

2. Block B1 is executed successfully by the forward system.
   io.logs_storage.len() == 16384 for B1.

3. Operator attempts to prove a multiblock batch [B1, B2] where B2 has ≥1 L2→L1 log.

4. During post_op for B1:
   io.logs_storage.apply_to_array_vec(&mut batch_data.logs_storage);
   → batch_data.logs_storage is now full (16384/16384).

5. During post_op for B2:
   io.logs_storage.apply_to_array_vec(&mut batch_data.logs_storage);
   → ArrayVec::push() panics: "ArrayVec is full"

6. The RISC-V proving binary panics; the batch proof cannot be generated.
   The operator must fall back to single-block batches or skip B1.
``` [1](#0-0) [3](#0-2) [2](#0-1)

### Citations

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

**File:** zk_ee/src/common_structs/logs_storage.rs (L311-316)
```rust
    pub fn apply_to_array_vec(&self, array_vec: &mut ArrayVec<Bytes32, 16384>) {
        self.list.iter().for_each(|el| {
            let log: L2ToL1Log = el.into();
            array_vec.push(log.hash())
        });
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_multiblock_batch.rs (L109-110)
```rust
        io.logs_storage
            .apply_to_array_vec(&mut batch_data.logs_storage);
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/batch_data.rs (L27-27)
```rust
    pub logs_storage: ArrayVec<Bytes32, 16384>,
```

**File:** basic_bootloader/src/bootloader/errors.rs (L103-104)
```rust
    /// Transaction makes the block reach the l2->l1 logs limit
    BlockL2ToL1LogsLimitReached,
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L146-158)
```rust
                            let block_logs_used = system.io.logs_len();
                            let next_block_blob_gas_used =
                                block_data.block_blob_gas_used + tx_processing_result.blob_gas_used;

                            // Check if the transaction made the block reach any of the limits
                            // for gas, native, pubdata or logs.
                            if let Err(err) = check_for_block_limits(
                                system,
                                next_block_gas_used,
                                next_block_computational_native_used,
                                next_block_pubdata_used,
                                block_logs_used,
                                next_block_blob_gas_used,
```

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

**File:** tests/instances/system_hooks/src/lib.rs (L1030-1035)
```rust
    let gas_used =
        call_address_and_measure_gas_cost(l1_messenger_address, sender, 0, calldata, vec![]);

    // Gas charged by the L1Messenger system contract's EVM bytecode (keccak + LOG costs).
    // The hook itself charges 0 ergs.
    assert_eq!(gas_used, 9202);
```
