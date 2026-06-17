### Title
Missing Emitter Address Validation in Event Hooks Allows Unauthorized Settlement Layer Chain ID Update and Interop Root Injection — (`system_hooks/src/event_hooks/system_context.rs`, `system_hooks/src/event_hooks/interop_root_reporter.rs`)

---

### Summary

The two system event hooks — `system_context_event_hook` and `interop_root_reporter_event_hook` — perform critical state mutations (`update_settlement_layer_chain_id` and `add_interop_root`) without any validation of the address that emitted the triggering event. Unlike every call hook in the codebase, which explicitly checks `caller`, the `SystemEventHook` function signature does not receive the emitter address at all, making it structurally impossible for these hooks to enforce access control. Any contract deployed at the registered system address that exposes a publicly callable function emitting the matching event signature can trigger these state mutations without restriction.

---

### Finding Description

**Call hooks** in ZKsync OS uniformly validate the caller before performing privileged operations:

- `l1_messenger_hook`: `if caller != L1_MESSENGER_ADDRESS { return empty; }`
- `mint_base_token_hook`: `if caller != L2_BASE_TOKEN_ADDRESS { return empty; }`
- `set_bytecode_on_address_hook`: `if caller != CONTRACT_DEPLOYER_ADDRESS && caller != COMPLEX_UPGRADER_ADDRESS { return empty; }`
- `contract_deployer_temp_hook`: `if caller != COMPLEX_UPGRADER_ADDRESS { return empty; }` [1](#0-0) [2](#0-1) 

**Event hooks** have no equivalent check. The `SystemEventHook` function type does not include the emitter address in its signature:

```rust
pub struct SystemEventHook<S: SystemTypes>(
    for<'a> fn(
        &arrayvec::ArrayVec<...>,  // topics
        &[u8],                      // data
        u8,                         // caller_ee only — NO emitter address
        &mut System<S>,
        &mut S::Resources,
    ) -> Result<(), SystemError>,
);
``` [3](#0-2) 

`try_intercept_event` dispatches to the hook using only `address_low` (the emitting contract's address) as a routing key, but does not forward the emitter address into the hook body: [4](#0-3) 

**`system_context_event_hook`** (`SYSTEM_CONTEXT_ADDRESS` = `0x800b`): Upon receiving any event with topic[0] == `SL_CHAIN_ID_UPDATED_EVENT_SIG`, it unconditionally calls `system.io.update_settlement_layer_chain_id(...)` with the attacker-controlled value from `topics[1]`. There is no check on who emitted the event: [5](#0-4) 

**`interop_root_reporter_event_hook`** (`L2_INTEROP_ROOT_STORAGE_ADDRESS` = `0x10008`): Upon receiving any event with topic[0] == `INTEROP_ROOT_ADDED_EVENT_SIG`, it unconditionally calls `system.io.add_interop_root(...)` with attacker-controlled `root`, `chain_id`, and `block_or_batch_number` from the event payload. No emitter validation: [6](#0-5) 

The hooks are registered for their respective system addresses: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

**`system_context_event_hook`**: An attacker who can make the contract at `0x800b` emit `SettlementLayerChainIdUpdated(uint256)` with an arbitrary value can overwrite the settlement layer chain ID — a critical system parameter governing cross-chain settlement. Corrupting this value disrupts the entire settlement layer routing.

**`interop_root_reporter_event_hook`**: An attacker who can make the contract at `0x10008` emit `InteropRootAdded(uint256,uint256,bytes32[])` with attacker-controlled data can inject arbitrary interop roots into the system. Fake interop roots can be used to forge cross-chain messages, potentially enabling unauthorized token transfers or replay attacks across chains.

Both mutations are performed directly on the `IOSubsystem` with no rollback path triggered by the hook itself.

---

### Likelihood Explanation

The attack requires the system contract deployed at `0x800b` or `0x10008` to expose a publicly callable function that emits the matching event signature. The hook itself provides zero resistance — it performs no emitter validation whatsoever. The `SystemEventHook` type structurally cannot be extended to add such a check without a signature change, because the emitter address is never passed to the hook. Any publicly callable path in the system contracts at those addresses that emits the matching event is immediately exploitable by any unprivileged transaction sender.

---

### Recommendation

1. **Extend `SystemEventHook` to include the emitter address** in its function signature, mirroring how call hooks receive `caller` from `ExternalCallRequest`:

```rust
pub struct SystemEventHook<S: SystemTypes>(
    for<'a> fn(
        &arrayvec::ArrayVec<...>,
        &[u8],
        u8,
        &<S::IOTypes as SystemIOTypesConfig>::Address,  // emitter address
        &mut System<S>,
        &mut S::Resources,
    ) -> Result<(), SystemError>,
);
```

2. **Add emitter address checks** in both hooks, analogous to call hook patterns:

```rust
// In system_context_event_hook:
if emitter != SYSTEM_CONTEXT_ADDRESS {
    return Ok(());
}

// In interop_root_reporter_event_hook:
if emitter != L2_INTEROP_ROOT_STORAGE_ADDRESS {
    return Ok(());
}
```

3. **Pass the emitter address** through `try_intercept_event` and `System::emit_event`. [9](#0-8) 

---

### Proof of Concept

1. Deploy a contract at `SYSTEM_CONTEXT_ADDRESS` (`0x800b`) — or exploit any publicly callable function on the existing contract there — that emits:
   ```
   emit SettlementLayerChainIdUpdated(attacker_controlled_chain_id)
   ```
   Topic[0] = `0x208daf0b9291c1e9a1697737d736630c808045f81f5bc5ae7b8ed740eb5a4d7a`, Topic[1] = attacker's chain ID.

2. The EVM interpreter processes the LOG opcode, calls `System::emit_event` with `address = 0x800b`.

3. `try_intercept_event` matches `address_low = 0x800b` to the registered `system_context_event_hook`.

4. `system_context_event_hook` checks `topics[0] == SL_CHAIN_ID_UPDATED_EVENT_SIG` — matches — and calls `system.io.update_settlement_layer_chain_id(attacker_controlled_chain_id)` with no further validation.

5. The settlement layer chain ID is permanently overwritten to the attacker's value for the remainder of the block.

The same pattern applies to `interop_root_reporter_event_hook` at `0x10008` with `InteropRootAdded` events, injecting fake interop roots. [10](#0-9) [11](#0-10)

### Citations

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

**File:** zk_ee/src/common_structs/system_hooks.rs (L52-60)
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
```

**File:** zk_ee/src/common_structs/system_hooks.rs (L152-170)
```rust
    pub fn try_intercept_event(
        &mut self,
        address_low: u32,
        topics: &arrayvec::ArrayVec<
            <S::IOTypes as SystemIOTypesConfig>::EventKey,
            MAX_EVENT_TOPICS,
        >,
        data: &[u8],
        caller_ee: u8,
        system: &mut System<S>,
        resources: &mut S::Resources,
    ) -> Result<Option<()>, SystemError> {
        let Some(hook) = self.event_hooks.get(&address_low) else {
            return Ok(None);
        };
        hook.0(topics, data, caller_ee, system, resources)?;

        Ok(Some(()))
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

**File:** system_hooks/src/lib.rs (L251-267)
```rust
pub fn add_interop_root_reporter<S: EthereumLikeTypes, A: Allocator + Clone>(
    hooks: &mut HooksStorage<S, A>,
) -> Result<(), InternalError> {
    hooks.add_event_hook(
        L2_INTEROP_ROOT_STORAGE_ADDRESS_LOW,
        SystemEventHook::new(interop_root_reporter_event_hook),
    )
}

pub fn add_system_context_reporter<S: EthereumLikeTypes, A: Allocator + Clone>(
    hooks: &mut HooksStorage<S, A>,
) -> Result<(), InternalError> {
    hooks.add_event_hook(
        SYSTEM_CONTEXT_ADDRESS_LOW,
        SystemEventHook::new(system_context_event_hook),
    )
}
```

**File:** system_hooks/src/addresses_constants.rs (L37-66)
```rust
pub const L2_INTEROP_ROOT_STORAGE_ADDRESS_LOW: u32 = 0x10008;
pub const L2_INTEROP_ROOT_STORAGE_ADDRESS: B160 =
    B160::from_limbs([L2_INTEROP_ROOT_STORAGE_ADDRESS_LOW as u64, 0, 0]);

// L2 interop center system contract
pub const L2_INTEROP_CENTER_ADDRESS_LOW: u32 = 0x1000d;
pub const L2_INTEROP_CENTER_ADDRESS: B160 =
    B160::from_limbs([L2_INTEROP_CENTER_ADDRESS_LOW as u64, 0, 0]);

// L2 asset tracker contract
pub const L2_ASSET_TRACKER_ADDRESS: B160 = B160::from_limbs([0x1000f, 0, 0]);

// Treasury contract used for "minting" base tokens on L2
pub const BASE_TOKEN_HOLDER_ADDRESS_LOW: u32 = 0x10011;
pub const BASE_TOKEN_HOLDER_ADDRESS: B160 =
    B160::from_limbs([BASE_TOKEN_HOLDER_ADDRESS_LOW as u64, 0, 0]);

// ERA VM system contracts (in fact we need implement only the methods that should be available for user contracts)
// TODO: may be better to implement as ifs inside EraVM EE
pub const ACCOUNT_CODE_STORAGE_STORAGE_ADDRESS: B160 = B160::from_limbs([0x8002, 0, 0]);
pub const KNOWN_CODE_STORAGE_ADDRESS: B160 = B160::from_limbs([0x8004, 0, 0]);
pub const IMMUTABLE_SIMULATOR_ADDRESS: B160 = B160::from_limbs([0x8005, 0, 0]);
// TODO: is a contract?
pub const FORCE_DEPLOYER_ADDRESS: B160 = B160::from_limbs([0x8007, 0, 0]);
pub const MSG_VALUE_SIMULATOR_ADDRESS: B160 = B160::from_limbs([0x8009, 0, 0]);
pub const BASE_TOKEN_ADDRESS: B160 = B160::from_limbs([0x800a, 0, 0]);

pub const SYSTEM_CONTEXT_ADDRESS_LOW: u32 = 0x800b;
pub const SYSTEM_CONTEXT_ADDRESS: B160 =
    B160::from_limbs([SYSTEM_CONTEXT_ADDRESS_LOW as u64, 0, 0]);
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
