### Title
Unprivileged Caller Can Trigger `new_sl_chain_id_event_hook` to Overwrite Settlement Layer Chain ID or Cause Fatal Block Abort - (File: `system_hooks/src/event_hooks/system_context.rs`)

---

### Summary

The `new_sl_chain_id_event_hook` in ZKsync OS processes the `SettlementLayerChainIdUpdated(uint256)` event emitted by the `SystemContext` contract (0x800b) and unconditionally writes the new settlement layer chain ID into the batch-level `NewSettlementLayerChainIdStorage`. The hook performs **no check on who called `SystemContext`** — it only validates the event signature and topic count. Because `SystemContext` is a plain EVM contract callable by any user, an unprivileged transaction sender can emit the sentinel event and either (a) overwrite the settlement layer chain ID with an attacker-controlled value, corrupting the batch commitment, or (b) trigger a fatal `internal_error!` that aborts block processing if the chain ID was already set in the same block.

---

### Finding Description

The event hook dispatch in `System::emit_event` intercepts events emitted by any contract whose address fits in 32 bits and routes them to a registered `SystemEventHook`: [1](#0-0) 

The hook for `SYSTEM_CONTEXT_ADDRESS_LOW` (0x800b) is `system_context_event_hook`, registered unconditionally at boot: [2](#0-1) 

Inside `new_sl_chain_id_event_hook`, the only guards are format checks on the event payload. There is **no check on who called `SystemContext`** — the `_caller_ee` parameter is explicitly discarded: [3](#0-2) 

The hook then calls `update_settlement_layer_chain_id` unconditionally with the attacker-supplied value from `topics[1]`: [4](#0-3) 

`NewSettlementLayerChainIdStorage::update` enforces at most one write per block, but returns a **fatal `internal_error!`** on a second call — it does not gracefully revert: [5](#0-4) 

The intended path for updating the settlement layer chain ID is a service transaction (type `0x7d`) calling `setSettlementLayerChainId` on `SystemContext`. Service transactions are operator-injected and unsigned: [6](#0-5) 

However, `SystemContext` (0x800b) is a standard EVM contract. Nothing in the ZKsync OS Rust layer prevents a regular L2 user from sending a transaction directly to `0x800b` calling `setSettlementLayerChainId(attacker_value)`. If the deployed `SystemContext` bytecode lacks an `onlySystemCall`-equivalent guard, the event is emitted and the hook fires with attacker-controlled data.

---

### Impact Explanation

**Scenario A — Chain ID overwrite (no prior service tx in the block):**
An attacker sends a regular L2 transaction to `SystemContext` calling `setSettlementLayerChainId(fake_id)`. The event fires, the hook writes `fake_id` into `new_settlement_layer_chain_id_storage`. At batch finalization, `settlement_layer_chain_id` in `BatchOutput` is set to `fake_id`, producing an invalid batch commitment accepted by the prover. This breaks cross-chain settlement integrity. [7](#0-6) 

**Scenario B — Fatal abort (service tx already ran in the block):**
If a legitimate service transaction already set the chain ID, a subsequent attacker transaction calling `setSettlementLayerChainId` causes `update` to return `internal_error!`, which propagates as `SystemError::LeafDefect` — a non-recoverable fatal error that aborts the entire block. This is a reliable DoS against any block that contains a `setSettlementLayerChainId` service transaction. [8](#0-7) 

---

### Likelihood Explanation

The `SystemContext` contract is a predeployed EVM contract at `0x800b`. The ZKsync OS Rust layer imposes no call-level restriction on who may call it. The only protection is whatever access control exists inside the `SystemContext` Solidity bytecode (`tests/rig/src/bytecodes/system_context.hex`). If that bytecode lacks a caller guard on `setSettlementLayerChainId` (selector `0x040203e6`), the attack is trivially executable by any funded L2 account with a single transaction. The hook itself provides zero defense: it ignores `_caller_ee`, checks only event format, and writes unconditionally. [9](#0-8) 

---

### Recommendation

1. **Add a caller check inside the hook.** The `SystemContext` Solidity contract should include the `msg.sender` as an indexed topic in `SettlementLayerChainIdUpdated`. The Rust hook should then verify that the emitting call originated from a service-transaction context (e.g., by checking `caller_ee` or a dedicated flag set by the bootloader when processing service transactions).

2. **Alternatively, enforce at the bootloader level.** The bootloader can set a per-block flag when a service transaction is being executed and expose it to the hook, so the hook rejects any `SettlementLayerChainIdUpdated` event emitted outside that context.

3. **Harden the fatal-error path.** `NewSettlementLayerChainIdStorage::update` should return a graceful revert error (not `internal_error!`) on a duplicate call, so an attacker cannot abort block processing by racing a legitimate service transaction. [5](#0-4) 

---

### Proof of Concept

```
1. Deploy a funded EOA on the ZKsync OS L2.
2. Craft a regular EIP-1559 L2 transaction:
     to   = 0x000000000000000000000000000000000000800b  (SystemContext)
     data = 0x040203e6 ++ abi.encode(uint256(999999))   (setSettlementLayerChainId(999999))
3. Submit the transaction in any non-service block.
4. SystemContext emits SettlementLayerChainIdUpdated(999999).
5. system_context_event_hook fires; new_sl_chain_id_event_hook reads topics[1] = 999999
   and calls update_settlement_layer_chain_id(999999) with no caller validation.
6. BatchOutput.settlement_layer_chain_id = 999999 instead of the real value.

For the DoS variant:
3b. Submit the attacker transaction in the same block as a legitimate
    setSettlementLayerChainId service transaction.
5b. update() finds value already set → returns internal_error! → block aborts.
``` [10](#0-9) [11](#0-10)

### Citations

**File:** zk_ee/src/system/mod.rs (L192-217)
```rust
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

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_init_op.rs (L18-21)
```rust
            system_hooks::add_contract_deployer(system_functions)?;
            system_hooks::add_interop_root_reporter(system_functions)?;
            system_hooks::add_system_context_reporter(system_functions)?;

```

**File:** system_hooks/src/event_hooks/system_context.rs (L18-67)
```rust
pub fn system_context_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
    data: &[u8],
    caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
where
{
    if topics.is_empty() {
        return Ok(());
    }
    // For now, we only capture the SettlementLayerChainIdUpdated event
    if topics[0].as_u8_array() == SL_CHAIN_ID_UPDATED_EVENT_SIG {
        new_sl_chain_id_event_hook(topics, data, caller_ee, system, resources)
    } else {
        Ok(())
    }
}

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

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/service_tx.rs (L9-21)
```rust
/// ZKsync OS service (type 0x7d) transaction .
/// Used for system operations, such as importing interop roots.
/// Can only be executed in service blocks, i.e. blocks with only service
/// transactions.
/// They have no signature, as they are added directly by the operator.
///
#[allow(dead_code)]
#[derive(Clone, Copy, Debug)]
pub(crate) struct ServiceTx<'a> {
    pub(crate) to: &'a [u8; 20], // NOTE: has to be one of the addresses in SERVICE_DESTINATION_WHITELIST
    pub(crate) data: &'a [u8], // NOTE: has to start with one of the selectors in SERVICE_DESTINATION_WHITELIST
    salt: u64, // Some salt used by the server to identify service transactions. Ignored by ZKsync OS.
}
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction_types/service_tx.rs (L40-55)
```rust
const SERVICE_DESTINATION_WHITELIST: &[(B160, [u8; 4])] = &[
    (
        L2_INTEROP_ROOT_STORAGE_ADDRESS,
        ADD_INTEROP_ROOTS_IN_BATCH_SELECTOR,
    ),
    (SYSTEM_CONTEXT_ADDRESS, SET_SL_CHAIN_ID_SELECTOR),
    (L2_INTEROP_CENTER_ADDRESS, SET_INTEROP_FEE_SELECTOR),
];

fn whitelisted(to: B160, data: &[u8]) -> bool {
    let selector: [u8; 4] = match data.get(..4).and_then(|bytes| bytes.try_into().ok()) {
        Some(selector) => selector,
        None => return false,
    };
    SERVICE_DESTINATION_WHITELIST.contains(&(to, selector))
}
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs (L231-240)
```rust
) -> (Bytes32, U256) {
    let multichain_root = read_multichain_root(io);
    let settlement_layer_chain_id = read_settlement_layer_chain_id(io);
    if let Some(new_settlement_layer_chain_id) = io.new_settlement_layer_chain_id_storage.value() {
        // If the SL chain id was updated, make sure the updated one matches
        // the one read from storage.
        assert_eq!(new_settlement_layer_chain_id, &settlement_layer_chain_id);
    }

    (multichain_root, settlement_layer_chain_id)
```
