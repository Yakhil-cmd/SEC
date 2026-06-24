### Title
SNS Extension Operations Permanently Blocked When WASM Removed from ALLOWED_EXTENSIONS — (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS governance extension system applies the same `ALLOWED_EXTENSIONS` whitelist check to both **registering** and **executing operations on** a registered extension. When an extension's WASM hash is removed from `ALLOWED_EXTENSIONS` (as explicitly happened with KongSwap on April 6, 2026), any SNS that had previously registered that extension can no longer execute operations on it — including `withdraw` — permanently locking treasury funds deposited into the extension. There is no `DeregisterExtension` proposal action to escape this state.

---

### Finding Description

The `ALLOWED_EXTENSIONS` thread-local map in `rs/sns/governance/src/extensions.rs` is the sole gating mechanism for which extension WASMs are permitted. It is currently intentionally empty:

```rust
thread_local! {
    static ALLOWED_EXTENSIONS: RefCell<BTreeMap<[u8; 32], ExtensionSpec>> = const { RefCell::new(btreemap! {
        // This collection is intentionally left empty. The Kong Swap extension used to be here,
        // but they ceased operations on April 6, 2026. Consequently, that was removed
        // from this list.
    }) };
}
``` [1](#0-0) 

This check is applied symmetrically to three distinct operations:

**1. Registration** — `validate_register_extension` calls `validate_extension_wasm` on the proposed WASM hash: [2](#0-1) 

**2. Operation execution** — `validate_execute_extension_operation` calls `get_extension_spec_and_update_cache`, which fetches the currently installed WASM hash from the extension canister and calls `validate_extension_wasm` on it: [3](#0-2) [4](#0-3) 

**3. Upgrade** — `validate_upgrade_extension` calls `validate_extension_wasm` on the new WASM hash: [5](#0-4) 

The `validate_extension_wasm` function rejects any hash not present in `ALLOWED_EXTENSIONS`: [6](#0-5) 

Critically, the SNS proposal `Action` enum contains `RegisterExtension` (Id=17), `ExecuteExtensionOperation` (Id=18), and `UpgradeExtension` (Id=19), but **no `DeregisterExtension` action**: [7](#0-6) 

The `withdraw` operation on a TreasuryManager extension can only be triggered through `perform_execute_extension_operation`, which calls `validate_execute_extension_operation`: [8](#0-7) 

There is no alternative governance path to call the extension canister's `withdraw` method directly.

---

### Impact Explanation

Any SNS that:
1. Registered a KongSwap TreasuryManager extension while its WASM hash was in `ALLOWED_EXTENSIONS`, and
2. Deposited SNS or ICP treasury funds into it via `ExecuteExtensionOperation{operation_name: "deposit"}`

…is now permanently unable to:
- Execute `withdraw` to recover deposited funds (blocked by `validate_execute_extension_operation` → `get_extension_spec_and_update_cache` → `validate_extension_wasm` failing)
- Upgrade the extension to a new version (blocked by `validate_upgrade_extension` requiring the new WASM to also be in `ALLOWED_EXTENSIONS`)
- Deregister the extension (no such proposal action exists)

The deposited treasury funds are permanently inaccessible through the normal SNS governance flow. The extension canister remains registered with Root and controlled by Root and Governance, but no governance proposal can recover the funds.

---

### Likelihood Explanation

This is not theoretical. The KongSwap extension was explicitly removed from `ALLOWED_EXTENSIONS` on April 6, 2026 (as documented in the source comment). Any SNS that had registered and funded a KongSwap TreasuryManager extension before that date is now in this locked state. The entry path is a standard SNS governance proposal submitted by any neuron holder — no privileged access is required to trigger the deposit that leads to the locked state.

---

### Recommendation

1. **Add a `DeregisterExtension` proposal action** that removes an extension from the SNS Root registry without requiring the extension's WASM to be in `ALLOWED_EXTENSIONS`. This is the direct analog to the M-16 fix: allow removal without a whitelist check.

2. **Exempt `withdraw` operations from the `ALLOWED_EXTENSIONS` check** in `validate_execute_extension_operation`. Withdrawal is a risk-reducing operation (recovering funds) and should not be blocked by the same gate that prevents new deposits. The check at line 1443 should be skipped or softened for operations classified as withdrawals.

3. **Provide a migration path** when removing an extension from `ALLOWED_EXTENSIONS`: before removal, SNSes with registered instances of that extension should be notified and given a governance window to withdraw funds.

---

### Proof of Concept

```
1. SNS submits RegisterExtension proposal for KongSwap (WASM hash in ALLOWED_EXTENSIONS).
   → validate_register_extension succeeds, extension registered with Root.

2. SNS submits ExecuteExtensionOperation{operation_name: "deposit", ...} proposal.
   → validate_execute_extension_operation succeeds (WASM hash still in ALLOWED_EXTENSIONS).
   → SNS treasury funds transferred to extension canister.

3. KongSwap WASM hash removed from ALLOWED_EXTENSIONS (April 6, 2026).
   → ALLOWED_EXTENSIONS is now empty.

4. SNS submits ExecuteExtensionOperation{operation_name: "withdraw", ...} proposal.
   → validate_execute_extension_operation calls get_extension_spec_and_update_cache.
   → get_extension_spec_and_update_cache fetches installed WASM hash from extension canister.
   → validate_extension_wasm(installed_hash) fails: "Wasm module with hash ... is not allowed as an extension."
   → Proposal fails validation. Funds remain locked in extension canister.

5. SNS submits UpgradeExtension proposal to replace with a new WASM.
   → validate_upgrade_extension calls validate_extension_wasm(new_hash).
   → Fails: new WASM hash also not in ALLOWED_EXTENSIONS (list is empty).
   → No upgrade path available.

6. No DeregisterExtension action exists. Extension remains registered. Funds permanently inaccessible.
```

### Citations

**File:** rs/sns/governance/src/extensions.rs (L48-54)
```rust
thread_local! {
    static ALLOWED_EXTENSIONS: RefCell<BTreeMap<[u8; 32], ExtensionSpec>> = const { RefCell::new(btreemap! {
        // This collection is intentionally left empty. The Kong Swap extension used to be here,
        // but they ceased operations on April 6, 2026. Consequently, that was removed
        // from this list.
    }) };
}
```

**File:** rs/sns/governance/src/extensions.rs (L952-959)
```rust
    if let Some(spec) = allowed_extensions.get(&hash_array) {
        return Ok(spec.clone());
    }

    Err(format!(
        "Wasm module with hash {:?} is not allowed as an extension.",
        hex::encode(wasm_module_hash)
    ))
```

**File:** rs/sns/governance/src/extensions.rs (L1141-1142)
```rust
    let spec = validate_extension_wasm(&wasm_module_hash)
        .map_err(|err| format!("Invalid extension wasm: {err}"))?;
```

**File:** rs/sns/governance/src/extensions.rs (L1276-1278)
```rust
    // Validate the new WASM against ALLOWED_EXTENSIONS
    let new_spec = validate_extension_wasm(&wasm_module_hash)
        .map_err(|err| format!("Invalid extension wasm: {err}"))?;
```

**File:** rs/sns/governance/src/extensions.rs (L1443-1451)
```rust
    let result = validate_extension_wasm(&wasm_module_hash).map_err(|err| {
        GovernanceError::new_with_message(
            ErrorType::InvalidProposal,
            format!(
                "Extension canister {extension_canister_id} does not have an extension spec \
                    despite being registered with Root: {err}",
            ),
        )
    });
```

**File:** rs/sns/governance/src/extensions.rs (L1502-1507)
```rust
    let extension_spec = get_extension_spec_and_update_cache(
        &*governance.env,
        governance.proto.root_canister_id_or_panic(),
        extension_canister_id,
    )
    .await?;
```

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L741-753)
```rust
        /// Register an SNS extension canister.
        ///
        /// Id = 17.
        RegisterExtension(super::RegisterExtension),
        /// Execute an SNS extension's operation.
        ///
        /// Id = 18.
        ExecuteExtensionOperation(super::ExecuteExtensionOperation),
        /// Upgrade an SNS extension canister.
        ///
        /// Id = 19.
        UpgradeExtension(super::UpgradeExtension),
    }
```

**File:** rs/sns/governance/src/governance.rs (L2558-2576)
```rust
    async fn perform_execute_extension_operation(
        &self,
        execute_extension_operation: ExecuteExtensionOperation,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }

        let validated_operation =
            validate_execute_extension_operation(self, execute_extension_operation).await?;

        // Execute the validated operation
        validated_operation.execute(self).await?;

        Ok(())
```
