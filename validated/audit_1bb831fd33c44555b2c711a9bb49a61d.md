### Title
Unbounded `InteropRootStorage` Growth Without Block-Level Cap Enables Proving-Phase Resource Exhaustion - (File: `zk_ee/src/common_structs/interop_root_storage.rs`)

---

### Summary

`InteropRootStorage::push_root` appends entries to an unbounded `HistoryList` with no maximum-count guard. The ZK proving path iterates over every stored interop root at block finalization to compute a rolling Keccak256 hash. An unprivileged caller who can trigger the `InteropRootAdded` event hook can fill this list within a single block, making the proving-phase iteration arbitrarily expensive and potentially exceeding the prover's computation budget, causing valid-execution unprovability or a forward/proving divergence.

---

### Finding Description

**Root cause — no size cap on `InteropRootStorage`:**

`push_root` unconditionally appends to the internal `HistoryList` and always returns `Ok(())`:

```rust
// zk_ee/src/common_structs/interop_root_storage.rs:41-45
pub fn push_root(&mut self, interop_root: InteropRoot) -> Result<(), SystemError> {
    self.list.push(interop_root, ());
    Ok(())
}
``` [1](#0-0) 

There is no `MAX_NUMBER_OF_INTEROP_ROOTS` constant, no length check before pushing, and no block-level limit analogous to `MAX_NUMBER_OF_LOGS = 16_384` that is enforced for L2→L1 logs. [2](#0-1) 

**Caller entry path — the `interop_root_reporter_event_hook`:**

Any transaction that emits an `InteropRootAdded` event from the designated system contract triggers `interop_root_reporter_event_hook`, which calls `system.io.add_interop_root(...)` → `self.interop_root_storage.push_root(interop_root)`. The hook validates the event signature and data format but imposes no per-block count limit: [3](#0-2) 

The `add_interop_root` implementation in `io_subsystem.rs` charges a fixed native cost per root (`INTEROP_ROOT_STORAGE_NATIVE_COST + per_root_computational_native_cost()`) and then calls `push_root` without any cap: [4](#0-3) 

**Proving-phase linear scan at finalization:**

At block finalization, `calculate_interop_roots_rolling_hash` iterates over every stored root:

```rust
// basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs:115
for root in roots {
    ...
    rolling_hash = hasher.finalize_reset().into()
}
``` [5](#0-4) 

This is called unconditionally during `PostTxOpProvingSingleblockBatch::post_op`: [6](#0-5) 

The per-root native charge is collected during transaction execution (forward mode), but the finalization-phase iteration happens **outside any resource-metered context** in the proving run. The prover's RISC-V cycle budget is therefore consumed proportionally to the number of stored roots, with no block-level ceiling to stop it.

**Contrast with the L2→L1 log limit:**

The ZK tx loop explicitly checks `logs_used > MAX_NUMBER_OF_LOGS` and reverts the offending transaction: [7](#0-6) 

No equivalent check exists for `interop_root_storage.len()`.

---

### Impact Explanation

- **Valid-execution unprovability / forward–proving divergence**: The forward (sequencer) run accepts a block containing many interop roots because per-root native cost is charged and the block's native limit is not exceeded. The proving run then iterates the full list during finalization outside the metered window, consuming unbounded RISC-V cycles. If the cycle count exceeds the prover's budget, the block cannot be proven, permanently stalling the chain.
- **Resource accounting bug**: The native cost charged per root during execution does not account for the O(N) finalization work, creating a systematic undercharge that grows with N.

---

### Likelihood Explanation

The `InteropRootAdded` event is emitted by a designated system contract. If that contract is callable by ordinary L2 transactions (or by a service/upgrade transaction the attacker can influence), a single transaction can emit many such events in a loop, each one appending to `InteropRootStorage`. The per-root native charge means the attacker pays proportionally, but the finalization overhead is not bounded by the block's native limit, so the attacker can craft a block that is accepted in forward mode but unprovable.

---

### Recommendation

1. Add a `MAX_NUMBER_OF_INTEROP_ROOTS` constant (analogous to `MAX_NUMBER_OF_LOGS`) and enforce it inside `push_root` or `add_interop_root`, returning a `SystemError` when exceeded.
2. Include `interop_root_storage.len()` in the ZK tx-loop's `check_for_block_limits` call so that a transaction pushing the count over the cap is reverted before it is committed, mirroring the existing log-limit enforcement.
3. Ensure the per-root native charge in `add_interop_root` accounts for the full amortized finalization cost, not just the per-push storage cost.

---

### Proof of Concept

1. Deploy or reuse the interop-root storage system contract.
2. Submit a single transaction that emits `N` `InteropRootAdded` events in a loop (e.g., N = 10,000), each with a distinct valid `(chain_id, block_or_batch_number, root)` triple.
3. The forward run accepts the block: per-root native cost is charged and the block's native limit is not breached.
4. The proving run reaches `calculate_interop_roots_rolling_hash` during `PostTxOpProvingSingleblockBatch::post_op` and iterates all N entries, each requiring a Keccak256 compression. With N large enough, the RISC-V cycle count exceeds the prover budget.
5. The block is accepted by the sequencer but cannot be proven, halting the chain.

### Citations

**File:** zk_ee/src/common_structs/interop_root_storage.rs (L41-45)
```rust
    pub fn push_root(&mut self, interop_root: InteropRoot) -> Result<(), SystemError> {
        self.list.push(interop_root, ());

        Ok(())
    }
```

**File:** zk_ee/src/common_structs/logs_storage.rs (L25-25)
```rust
pub const MAX_NUMBER_OF_LOGS: u64 = 16_384;
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L19-81)
```rust
pub fn interop_root_reporter_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
    data: &[u8],
    _caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
where
{
    // First, ensure we're capturing the InteropRootAdded event
    if topics.is_empty() || topics[0].as_u8_array() != INTEROP_ROOT_ADDED_EVENT_SIG {
        return Ok(());
    }
    // Internal error if the data supplied doesn't match the expected value
    if data.len() != 96 {
        return Err(internal_error!("Interop root reporter event hook received bad data").into());
    }

    // Parse data
    let offset: u32 = match U256::from_be_slice(&data[..32]).try_into() {
        Ok(offset) => offset,
        Err(_) => {
            return Err(
                internal_error!("Interop root reporter event hook received bad offset").into(),
            );
        }
    };
    // This event is part of the system, but we check it anyways
    if offset != 32 {
        return Err(internal_error!("Interop root reporter event hook received bad offset").into());
    }

    let len: u32 = match U256::from_be_slice(&data[32..64]).try_into() {
        Ok(offset) => offset,
        Err(_) => {
            return Err(
                internal_error!("Interop root reporter event hook received bad length").into(),
            );
        }
    };
    // It should have exactly one side
    if len != 1 {
        return Err(internal_error!("Interop root reporter event hook received bad length").into());
    }
    // Validate topics length
    if topics.len() != 3 {
        return Err(internal_error!("Interop root reporter event hook received bad topics").into());
    }

    let root = Bytes32::from_array(data[64..96].try_into().unwrap());
    let chain_id = U256::from_be_bytes(topics[1].as_u8_array());
    let block_or_batch_number = U256::from_be_bytes(topics[2].as_u8_array());
    system.io.add_interop_root(
        ExecutionEnvironmentType::NoEE,
        resources,
        InteropRoot {
            root,
            block_or_batch_number,
            chain_id,
        },
    )?;

    Ok(())
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L230-246)
```rust
    fn add_interop_root(
        &mut self,
        _ee_type: ExecutionEnvironmentType,
        resources: &mut Self::Resources,
        interop_root: InteropRoot,
    ) -> Result<(), SystemError> {
        // For native we charge for the storage and the computation of the rolling
        // hash (keccak of old hash || new root).
        let native = <Self::Resources as Resources>::Native::from_computational(
            INTEROP_ROOT_STORAGE_NATIVE_COST + per_root_computational_native_cost(),
        );

        let to_charge = Self::Resources::from_native(native);
        resources.charge(&to_charge)?;

        self.interop_root_storage.push_root(interop_root)
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs (L107-128)
```rust
pub fn calculate_interop_roots_rolling_hash<'a>(
    old_rolling_hash: Bytes32,
    roots: impl Iterator<Item = &'a InteropRoot>,
    hasher: &mut crypto::sha3::Keccak256,
) -> Bytes32 {
    let mut data = [0u8; 96];

    let mut rolling_hash = old_rolling_hash;
    for root in roots {
        data[0..32].copy_from_slice(&rolling_hash.as_u8_ref());
        data[32..64].copy_from_slice(&root.chain_id.to_be_bytes::<{ U256::BYTES }>());
        data[64..96].copy_from_slice(&root.block_or_batch_number.to_be_bytes::<{ U256::BYTES }>());
        hasher.update(data);

        // Note: now we have only one side
        hasher.update(root.root.as_u8_ref());

        rolling_hash = hasher.finalize_reset().into()
    }

    rolling_hash
}
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs (L114-118)
```rust
        let interop_roots_rolling_hash = calculate_interop_roots_rolling_hash(
            Bytes32::zero(),
            io.interop_root_storage.iter(),
            &mut crypto::sha3::Keccak256::new(),
        );
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
