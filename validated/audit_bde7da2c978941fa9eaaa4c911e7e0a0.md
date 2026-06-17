### Title
Unconstrained Emitter in `system_context_event_hook` Allows Unauthorized Settlement Layer Chain ID Manipulation — (`File: system_hooks/src/event_hooks/system_context.rs`)

---

### Summary

The `system_context_event_hook` event hook, which updates the settlement layer chain ID used in batch public inputs, performs **no validation of who triggered the event emission**. Unlike every call hook in the system (which explicitly checks `caller`), the event hook interface does not expose the emitter's caller address, and the hook itself does not check `caller_ee` or any other authorization signal. Any contract or user that can cause the SystemContext contract at `0x800b` to emit `SettlementLayerChainIdUpdated(uint256)` with an attacker-chosen value will have that value written into the system's settlement layer chain ID state — directly affecting the `BatchOutput` committed to the settlement layer.

---

### Finding Description

**Call hooks** in ZKsync OS all perform explicit caller validation:

- `l1_messenger_hook`: `if caller != L1_MESSENGER_ADDRESS { return empty; }` [1](#0-0) 
- `set_bytecode_on_address_hook`: `if caller != CONTRACT_DEPLOYER_ADDRESS && caller != COMPLEX_UPGRADER_ADDRESS { return empty; }` [2](#0-1) 
- `mint_base_token_hook`: `if caller != L2_BASE_TOKEN_ADDRESS { return empty; }` [3](#0-2) 

**Event hooks** have a fundamentally different interface — the `SystemEventHook` function signature receives only `topics`, `data`, `caller_ee`, `system`, and `resources`. The emitter's caller address is **never passed in**: [4](#0-3) 

The `system_context_event_hook` is registered for `SYSTEM_CONTEXT_ADDRESS_LOW` (`0x800b`) and fires whenever that address emits an event: [5](#0-4) 

Inside `new_sl_chain_id_event_hook`, the only checks are structural (data must be empty, topics must have length 2). There is **no check on who called the SystemContext contract** that caused the event to be emitted: [6](#0-5) 

The hook then unconditionally calls `system.io.update_settlement_layer_chain_id(...)` with the attacker-supplied value from `topics[1]`. [7](#0-6) 

The `emit_event` dispatch in `zk_ee/src/system/mod.rs` routes to the hook purely by address, with no caller context forwarded: [8](#0-7) 

The updated settlement layer chain ID flows directly into `BatchOutput.settlement_layer_chain_id`, which is committed in the batch public input: [9](#0-8) 

---

### Impact Explanation

If an unprivileged caller can invoke the SystemContext contract at `0x800b` and cause it to emit `SettlementLayerChainIdUpdated(uint256)` with an arbitrary value, the ZKsync OS hook will write that value as the settlement layer chain ID. This corrupts `BatchOutput`, causing:

1. **Batch rejection on the settlement layer** — the committed chain ID will not match the expected one, causing the proof to be rejected.
2. **Incorrect cross-chain accounting** — the `l1_chain_id` read from `L2AssetTracker` slot 154 and passed to `handleFinalizeBaseTokenBridgingOnL2` is a separate value, but the settlement layer chain ID mismatch breaks the batch commitment integrity. [10](#0-9) 

The `NewSettlementLayerChainIdStorage` enforces at most one update per block, so a second attempt in the same block would cause a fatal internal error — but the first attacker-controlled update would already be committed. [11](#0-10) 

---

### Likelihood Explanation

The exploitability depends on whether the SystemContext contract at `0x800b` enforces that only service transactions (which require `from == BOOTLOADER_FORMAL_ADDRESS`) can call `setSettlementLayerChainId`. The ZKsync OS hook itself provides **zero** defense-in-depth: it cannot check the caller because the `SystemEventHook` interface does not expose it. If the SystemContext contract's access control is ever weakened, bypassed, or if a re-entrancy path exists, the hook will accept the event unconditionally. The asymmetry with call hooks — which all have explicit caller guards — makes this a structural gap.

---

### Recommendation

1. **Extend the `SystemEventHook` interface** to pass the emitter's `caller` address (analogous to how `ExternalCallRequest` exposes `caller` for call hooks), so event hooks can perform caller validation.
2. **Add an explicit check in `new_sl_chain_id_event_hook`** that the event was emitted via a privileged call path (e.g., `caller_ee == NoEE` or a specific caller address), mirroring the pattern used in all call hooks.
3. **Document the security invariant** that `system_context_event_hook` relies entirely on the SystemContext contract's access controls, so any upgrade to that contract is reviewed with this dependency in mind.

---

### Proof of Concept

1. Attacker deploys a contract `A` that calls `SystemContext(0x800b).setSettlementLayerChainId(attacker_chain_id)`.
2. If the SystemContext contract does not enforce `msg.sender == BOOTLOADER_FORMAL_ADDRESS` at the EVM level, the call succeeds and emits `SettlementLayerChainIdUpdated(attacker_chain_id)`.
3. `emit_event` in `zk_ee/src/system/mod.rs` routes the event to `system_context_event_hook` because the emitter address is `0x800b`. [12](#0-11) 
4. `new_sl_chain_id_event_hook` passes the structural checks (data empty, 2 topics) and calls `update_settlement_layer_chain_id(attacker_chain_id)`. [7](#0-6) 
5. The corrupted chain ID is committed into `BatchOutput.settlement_layer_chain_id` at block finalization. [13](#0-12) 
6. The batch proof is generated with the wrong settlement layer chain ID, causing it to be rejected or accepted with incorrect cross-chain state.

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

**File:** system_hooks/src/call_hooks/set_bytecode_on_address.rs (L39-50)
```rust
    // Can be used only by Contract Deployer system contract or directly by complex upgrader
    if caller != CONTRACT_DEPLOYER_ADDRESS && caller != COMPLEX_UPGRADER_ADDRESS {
        system_log!(
            system,
            "Set bytecode hook: invalid caller (caller={caller:?})\n"
        );
        // Pretend to be an empty account
        return Ok((
            make_return_state_from_returndata_region(available_resources, &[]),
            return_memory,
        ));
    }
```

**File:** system_hooks/src/call_hooks/mint_base_token.rs (L39-46)
```rust
    // Only allow L2 base token contract to mint tokens
    if caller != L2_BASE_TOKEN_ADDRESS {
        // Pretend to be an empty account
        return Ok((
            make_return_state_from_returndata_region(available_resources, &[]),
            return_memory,
        ));
    }
```

**File:** zk_ee/src/common_structs/system_hooks.rs (L52-74)
```rust
pub struct SystemEventHook<S: SystemTypes>(
    for<'a> fn(
        &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
        &[u8],
        u8,
        &mut System<S>,
        &mut S::Resources,
    ) -> Result<(), SystemError>,
);

impl<S: SystemTypes> SystemEventHook<S> {
    pub fn new(
        f: for<'a> fn(
            &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
            &[u8],
            u8,
            &mut System<S>,
            &mut S::Resources,
        ) -> Result<(), SystemError>,
    ) -> Self {
        Self(f)
    }
}
```

**File:** system_hooks/src/lib.rs (L260-267)
```rust
pub fn add_system_context_reporter<S: EthereumLikeTypes, A: Allocator + Clone>(
    hooks: &mut HooksStorage<S, A>,
) -> Result<(), InternalError> {
    hooks.add_event_hook(
        SYSTEM_CONTEXT_ADDRESS_LOW,
        SystemEventHook::new(system_context_event_hook),
    )
}
```

**File:** system_hooks/src/event_hooks/system_context.rs (L38-67)
```rust
fn new_sl_chain_id_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
    data: &[u8],
    _caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
where
{
    // Internal error if the data supplied isn't empty
    if !data.is_empty() {
        return Err(
            internal_error!("New SL chain id reporter event hook received bad data").into(),
        );
    }
    // Same if there's a mismatch in expected topics
    if topics.len() != 2 {
        return Err(
            internal_error!("New SL chain id reporter event hook received bad topics").into(),
        );
    }

    let new_sl_chain_id = U256::from_be_bytes(topics[1].as_u8_array());
    system.io.update_settlement_layer_chain_id(
        ExecutionEnvironmentType::NoEE,
        resources,
        new_sl_chain_id,
    )?;

    Ok(())
```

**File:** zk_ee/src/system/mod.rs (L191-217)
```rust
    /// Emit an event, potentially capturing some using an event hook.
    pub fn emit_event(
        &mut self,
        hooks: &mut HooksStorage<S, S::Allocator>,
        ee_type: ExecutionEnvironmentType,
        resources: &mut S::Resources,
        address: &<S::IOTypes as SystemIOTypesConfig>::Address,
        topics: &ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
        data: &[u8],
    ) -> Result<(), SystemError> {
        // First, emit the event using io subsystem
        self.io
            .emit_event(ee_type, resources, address, topics, data)?;

        // If successful, intercept event hook, if any
        if let Some(address_low) = address.try_into_low() {
            let _ = hooks.try_intercept_event(
                address_low,
                topics,
                data,
                ee_type as u8,
                self,
                resources,
            )?;
        }
        Ok(())
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L52-80)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L917-944)
```rust
/// Reads L1 chain id from L2AssetTracker storage.
///
/// This is the chain tokens are bridged *from* during L1→L2 deposits,
/// passed as `_fromChainId` to `handleFinalizeBaseTokenBridgingOnL2`.
fn read_l1_chain_id<S: EthereumLikeTypes>(system: &mut System<S>) -> U256
where
    S::IO: IOSubsystemExt,
{
    // L2AssetTracker storage layout (verified via `forge inspect`):
    //   slots 0-100:   Initializable + OwnableUpgradeable + Ownable2StepUpgradeable
    //   slots 101-150: Ownable2Step __gap
    //   slot 151:      mapping chainBalance
    //   slot 152:      mapping assetMigrationNumber
    //   slot 153:      mapping isAssetRegistered
    //   slot 154:      uint256 L1_CHAIN_ID
    let l1_chain_id_slot = Bytes32::from_u256_be(&U256::from(154));
    let mut inf_resources = S::Resources::FORMAL_INFINITE;
    let chain_id = system
        .io
        .storage_read::<false>(
            ExecutionEnvironmentType::NoEE,
            &mut inf_resources,
            &L2_ASSET_TRACKER_ADDRESS,
            &l1_chain_id_slot,
        )
        .expect("must read L2AssetTracker L1_CHAIN_ID");
    U256::from_be_bytes(chain_id.as_u8_array())
}
```

**File:** zk_ee/src/common_structs/new_settlement_layer_chain_id_storage.rs (L41-51)
```rust
    pub fn update(&mut self, new_sl_chain_id: U256) -> Result<(), SystemError> {
        if self.value().is_some() {
            return Err(internal_error!(
                "Tried to update settlement layer chain id more than once in a block"
            )
            .into());
        }
        self.history.update(new_sl_chain_id);

        Ok(())
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/post_tx_op_proving_singleblock_batch.rs (L185-198)
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
```
