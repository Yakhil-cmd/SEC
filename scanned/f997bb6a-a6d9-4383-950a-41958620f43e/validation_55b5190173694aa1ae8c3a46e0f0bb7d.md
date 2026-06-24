### Title
SNS Governance `ManageLedgerParameters` Proposal Leaves `transaction_fee_e8s` Permanently Stale After Ledger Upgrade Confirmation Timeout, Bricking All Neuron Disburse/Split Operations - (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

`perform_manage_ledger_parameters` upgrades the SNS ledger canister with a new transfer fee, but only updates the governance-cached `NervousSystemParameters.transaction_fee_e8s` if an upgrade-confirmation polling loop succeeds within 5 minutes. If the loop times out, the ledger already holds the new fee while governance permanently retains the old value. Every subsequent neuron `disburse` or `split` call passes the stale (lower) fee to the ledger, which rejects the transfer, making all staked SNS tokens inaccessible.

---

### Finding Description

`perform_manage_ledger_parameters` executes in three phases:

**Phase 1 – Ledger upgrade (irreversible):**
```rust
self.upgrade_non_root_canister(
    ledger_canister_id,
    Wasm::Bytes(ledger_wasm),
    ledger_upgrade_arg,          // contains the new transfer_fee
    CanisterInstallMode::Upgrade,
)
.await?;
``` [1](#0-0) 

After this `await` returns `Ok`, the ledger canister is already running with the new fee. There is no rollback path.

**Phase 2 – Confirmation polling loop (can time out):**
```rust
let mark_failed_at_seconds = self.env.now() + 5 * 60;
loop {
    // poll canister_info, look for a matching CanisterCodeDeployment
    // whose module_hash == current_version.ledger_wasm_hash
    if self.env.now() > mark_failed_at_seconds {
        return Err(GovernanceError::new_with_message(ErrorType::External, error));
    }
}
``` [2](#0-1) 

The loop matches on `code_deployment.module_hash()[..] == current_version.ledger_wasm_hash[..]`. If a concurrent `UpgradeSnsToNextVersion` proposal executes between Phase 1 and Phase 2 (it does not set `pending_version` until after `check_no_upgrades_in_progress` is already past), the ledger's module hash changes and the confirmation check never matches, causing a 5-minute timeout.

**Phase 3 – Fee sync (only reached on success):**
```rust
if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
    && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
{
    nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
}
return Ok(());
``` [3](#0-2) 

If Phase 2 times out, Phase 3 is never reached. `transaction_fee_e8s` retains the old value permanently.

Every neuron operation then reads the stale fee:
```rust
pub(crate) fn transaction_fee_e8s_or_panic(&self) -> u64 {
    self.nervous_system_parameters_or_panic()
        .transaction_fee_e8s
        .expect("NervousSystemParameters must have transaction_fee_e8s")
}
``` [4](#0-3) 

`disburse_neuron` passes this stale value directly to the ledger:
```rust
let block_height = self
    .ledger
    .transfer_funds(
        disburse_amount_e8s,
        transaction_fee_e8s,   // ← stale old fee
        Some(from_subaccount),
        to_account,
        self.env.now(),
    )
    .await?;
``` [5](#0-4) 

The ICRC-1 ledger rejects any transfer whose supplied fee is below the actual fee. Because the ledger now enforces the new (higher) fee, every `disburse_neuron` and `split_neuron` call fails with an `InsufficientFee` error. Neuron stakes held in ledger subaccounts become permanently inaccessible until a separate `ManageNervousSystemParameters` proposal corrects `transaction_fee_e8s` — which itself requires another governance majority.

The `ManageLedgerParameters` proposal type is defined in the SNS governance proto and is a standard governance action: [6](#0-5) 

The `NervousSystemParameters.transaction_fee_e8s` field is the sole source of truth for the fee used in all neuron ledger calls: [7](#0-6) 

---

### Impact Explanation

All SNS neuron holders who have staked tokens lose the ability to disburse or split their neurons. The tokens remain locked in ledger subaccounts controlled by the governance canister principal. Recovery requires a subsequent `ManageNervousSystemParameters` proposal to re-sync `transaction_fee_e8s`, which requires another governance majority — potentially impossible if the SNS is in a degraded state or if token holders cannot coordinate. This is a direct, permanent loss of access to user funds until governance intervenes.

---

### Likelihood Explanation

The trigger condition — confirmation loop timeout — can occur in two realistic ways:

1. **Concurrent `UpgradeSnsToNextVersion` execution:** The `check_no_upgrades_in_progress` guard is evaluated only at the start of `perform_manage_ledger_parameters`. A concurrently adopted `UpgradeSnsToNextVersion` proposal can begin executing after this guard passes, upgrading the ledger to a new wasm hash before the confirmation loop finds its expected hash. The loop then spins for 5 minutes and returns `Err`.

2. **Transient management canister unavailability:** If `canister_info` calls to `CanisterId::ic_00()` fail repeatedly for 5 minutes (e.g., during a subnet slowdown), the loop times out without ever confirming the upgrade.

Both scenarios are plausible in production SNS deployments. The first is particularly realistic because `UpgradeSnsToNextVersion` is a routine operation and the 5-minute window is long.

---

### Recommendation

Update `nervous_system_parameters.transaction_fee_e8s` **immediately after `upgrade_non_root_canister` returns `Ok`**, before the confirmation polling loop, so the cached fee is always consistent with the ledger regardless of whether the confirmation loop succeeds or times out. Alternatively, remove the cached copy entirely and always query the ledger's actual fee at call time via `icrc1_fee`.

---

### Proof of Concept

1. SNS is initialized with `transaction_fee_e8s = 10_000`; ledger fee = 10,000.
2. Governance adopts `ManageLedgerParameters { transfer_fee: Some(1_000_000) }`.
3. `perform_manage_ledger_parameters` executes: `upgrade_non_root_canister` succeeds — ledger now enforces fee = 1,000,000.
4. Concurrently, `UpgradeSnsToNextVersion` executes and upgrades the ledger to a new wasm version (different hash).
5. The `ManageLedgerParameters` confirmation loop never finds `module_hash == current_version.ledger_wasm_hash`; it times out after 5 minutes and returns `Err`.
6. `transaction_fee_e8s` remains 10,000 in `NervousSystemParameters`.
7. A neuron holder calls `disburse_neuron`. Governance computes `transaction_fee_e8s = 10_000` and calls `ledger.transfer_funds(amount, 10_000, ...)`.
8. The ledger rejects with `BadFee { expected_fee: 1_000_000 }`.
9. All neuron disburse and split operations fail permanently. Staked tokens are inaccessible.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1214-1223)
```rust
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(from_subaccount),
                to_account,
                self.env.now(),
            )
            .await?;
```

**File:** rs/sns/governance/src/governance.rs (L3150-3156)
```rust
        self.upgrade_non_root_canister(
            ledger_canister_id,
            Wasm::Bytes(ledger_wasm),
            ledger_upgrade_arg,
            CanisterInstallMode::Upgrade,
        )
        .await?;
```

**File:** rs/sns/governance/src/governance.rs (L3158-3210)
```rust
        // If this operation takes 5 minutes, there is very likely a real failure, and other intervention will
        // be required
        let mark_failed_at_seconds = self.env.now() + 5 * 60;

        loop {
            let ledger_canister_info = self.env
                .call_canister(
                    CanisterId::ic_00(),
                    "canister_info",
                    candid::encode_one(
                        CanisterInfoRequest::new(
                            ledger_canister_id,
                            Some(20), // Get enough to ensure we did not miss the relevant change
                        )
                    ).map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not check if ledger upgrade succeeded. Error encoding canister_info request.\n{e}")))?
                )
                .await
                .map(|b| {
                    candid::decode_one::<CanisterInfoResponse>(&b)
                        .map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not check if ledger upgrade succeeded. Error decoding canister_info response.\n{e}")))
                })
                .map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not check if ledger upgrade succeeded. Canister method call canister_info failed: {e:?}")))??;

            for canister_change in ledger_canister_info.changes().iter().rev() {
                if canister_change.canister_version()
                    > ledger_canister_info_version_number_before_upgrade
                    && let CanisterChangeDetails::CanisterCodeDeployment(code_deployment) =
                        canister_change.details()
                    && let CanisterInstallMode::Upgrade = code_deployment.mode()
                    && code_deployment.module_hash()[..] == current_version.ledger_wasm_hash[..]
                {
                    // success
                    // update nervous-system-parameters transaction_fee if the fee is changed.
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
                    return Ok(());
                }
            }

            if self.env.now() > mark_failed_at_seconds {
                let error = format!(
                    "Upgrade marked as failed at {}. \
                     Did not find an upgrade in the ledger's canister_info recent_changes.",
                    format_timestamp_for_humans(self.env.now()),
                );
                return Err(GovernanceError::new_with_message(
                    ErrorType::External,
                    error,
                ));
            }
```

**File:** rs/sns/governance/src/governance.rs (L3369-3373)
```rust
    pub(crate) fn transaction_fee_e8s_or_panic(&self) -> u64 {
        self.nervous_system_parameters_or_panic()
            .transaction_fee_e8s
            .expect("NervousSystemParameters must have transaction_fee_e8s")
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L393-400)
```text
// A proposal function that changes the ledger's parameters.
// Fields with None values will remain unchanged.
message ManageLedgerParameters {
  optional uint64 transfer_fee = 1;
  optional string token_name = 2;
  optional string token_symbol = 3;
  optional string token_logo = 4;
}
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1129-1131)
```text
  // The transaction fee that must be paid for ledger transactions (except
  // minting and burning governance tokens).
  optional uint64 transaction_fee_e8s = 3;
```
