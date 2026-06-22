### Title
SNS Treasury Deposit 50% Cap Bypassed by Concurrent `ExecuteExtensionOperation` Proposals - (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

The `validate_deposit_operation_impl` function enforces a 50% cap on treasury deposits by checking the requested amount against the **current** treasury balance at validation time. However, because IC canister execution is async and multiple proposals can be spawned concurrently via `spawn_in_canister_env`, two `ExecuteExtensionOperation` proposals can each independently pass the 50% check against the same pre-transfer balance snapshot, and together transfer up to ~100% of the SNS treasury to the extension canister — completely bypassing the intended safety cap.

---

### Finding Description

In `rs/sns/governance/src/extensions.rs`, `validate_deposit_operation_impl` fetches the live treasury balance and checks that neither the SNS nor ICP deposit request exceeds 50% of the current balance:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(...)
}
if icp_requested > icp_balance.checked_div(2).unwrap() {
    return Err(...)
}
``` [1](#0-0) 

This validation is called at **execution time** inside `perform_execute_extension_operation`:

```rust
let validated_operation =
    validate_execute_extension_operation(self, execute_extension_operation).await?;
validated_operation.execute(self).await?;
``` [2](#0-1) 

Proposal execution is spawned as a background task via `spawn_in_canister_env`: [3](#0-2) 

Because IC canisters interleave execution at `await` points, two proposals executing concurrently will both call `validate_deposit_operation_impl` before either has completed its transfer. Both see the same pre-transfer balance and both pass the 50% check independently.

The actual transfer happens inside `execute_treasury_manager_deposit`, which contains multiple sequential `await` points: [4](#0-3) 

**Concrete attack scenario:**

- SNS treasury holds 100 SNS tokens and 200 ICP.
- Proposal A: deposit 49 SNS + 99 ICP (each < 50% of balance → passes validation).
- Proposal B: deposit 49 SNS + 99 ICP (each < 50% of balance → passes validation).
- Both proposals are adopted and begin executing concurrently.
- Proposal A validates (balance = 100 SNS / 200 ICP), passes, then yields at `treasury_manager_deposit_context().await`.
- Proposal B validates (balance still = 100 SNS / 200 ICP, no transfer has occurred yet), passes.
- Both proposals complete their `approve_treasury_manager` calls and call `deposit` on the extension canister.
- **Result**: 98 SNS + 198 ICP transferred — 98% of the SNS treasury and 99% of the ICP treasury — far exceeding the intended 50% cap.

The 50% check is structurally correct in isolation but does not account for other in-flight proposals that have already passed validation but have not yet completed their transfers.

---

### Impact Explanation

The 50% cap is a critical safety mechanism to prevent an SNS from depositing the majority of its treasury into an external extension canister (e.g., a DEX adaptor) in a single governance action. Bypassing this cap allows up to ~100% of the treasury to be transferred to the extension canister in a single coordinated governance action. If the extension canister is malicious, compromised, or has a bug, the entire SNS treasury could be lost. This is a **governance authorization bug** with direct financial impact on SNS token holders.

---

### Likelihood Explanation

An SNS with a whale neuron holding majority voting power (common in early-stage SNS deployments) can submit and pass two concurrent deposit proposals in the same voting period. The proposals need only be submitted close together so that both are adopted before either executes. This is a realistic and low-effort attack for any SNS controller with sufficient voting power, requiring no privileged access beyond normal SNS neuron ownership.

---

### Recommendation

Re-validate the 50% cap at the point of actual transfer, accounting for the **post-approval** balance (i.e., after any concurrent approvals have already reduced the treasury). Specifically:

1. Re-fetch the treasury balance immediately before calling `approve_treasury_manager` inside `execute_treasury_manager_deposit`, and re-check the 50% constraint at that point.
2. Alternatively, introduce a per-proposal lock or a global "pending deposit amount" counter that is atomically checked and incremented before any async yield, and decremented on completion or failure.
3. Consider limiting the number of concurrently executing `ExecuteExtensionOperation` proposals to one at a time using a canister-level lock (similar to the `update_balance_accounts` lock used in the ckBTC minter). [5](#0-4) 

---

### Proof of Concept

**Entry path**: Any SNS neuron holder with sufficient voting power submits two `ExecuteExtensionOperation` proposals with `operation_name = "deposit"` and `treasury_allocation_sns_e8s` each set to just under 50% of the current SNS treasury balance. Both proposals pass voting. Both begin executing concurrently via `spawn_in_canister_env`. Both call `validate_deposit_operation_impl` before either has transferred any tokens, both pass the 50% check against the same pre-transfer balance, and both complete their transfers — together exceeding the 50% cap.

**Relevant code path**:

1. `perform_action` → `Action::ExecuteExtensionOperation` → `perform_execute_extension_operation` [6](#0-5) 

2. `perform_execute_extension_operation` → `validate_execute_extension_operation` → `validate_deposit_operation_impl` (50% check against current balance) [7](#0-6) 

3. `validated_operation.execute` → `execute_treasury_manager_deposit` → `approve_treasury_manager` → actual ledger transfer [8](#0-7)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L276-321)
```rust
async fn validate_deposit_operation_impl(
    governance: &Governance,
    value: Option<Precise>,
) -> Result<ValidatedDepositOperationArg, String> {
    let structurally_valid = ValidatedDepositOperationArg::try_from(value)?;

    let sns_subaccount = governance.sns_treasury_subaccount();
    let icp_subaccount = governance.icp_treasury_subaccount();

    // Fail if either is asking for more than 50% of current balance.  The balance could have changed
    // since the proposal was created, and we don't assume that the proposal should work
    let sns_balance = governance
        .ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: sns_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get SNS treasury balance: {e:?}"))?;
    let icp_balance = governance
        .nns_ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: icp_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get ICP treasury balance: {e:?}"))?;

    let icp_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_icp_e8s);
    let sns_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_sns_e8s);

    // Unwrap is safe, only fails if divisor is zero, which we don't do.
    if sns_requested > sns_balance.checked_div(2).unwrap() {
        return Err(format!(
            "SNS treasury deposit request of {sns_requested} exceeds 50% of current SNS Token balance of {sns_balance}"
        ));
    }

    if icp_requested > icp_balance.checked_div(2).unwrap() {
        return Err(format!(
            "ICP treasury deposit request of {icp_requested} exceeds 50% of current ICP balance of {icp_balance}"
        ));
    }

    Ok(structurally_valid)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1546-1610)
```rust
async fn execute_treasury_manager_deposit(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedDepositOperationArg,
) -> Result<(), GovernanceError> {
    let ValidatedDepositOperationArg {
        treasury_allocation_sns_e8s,
        treasury_allocation_icp_e8s,
        original,
    } = arg;

    let context = governance.treasury_manager_deposit_context().await?;
    let arg_blob =
        construct_treasury_manager_deposit_payload(context, original).map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Failed to construct treasury manager deposit payload: {err}"),
            )
        })?;

    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
}
```

**File:** rs/sns/governance/src/governance.rs (L2132-2133)
```rust
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
```

**File:** rs/sns/governance/src/governance.rs (L2176-2179)
```rust
            Action::ExecuteExtensionOperation(execute_extension_operation) => {
                self.perform_execute_extension_operation(execute_extension_operation)
                    .await
            }
```

**File:** rs/sns/governance/src/governance.rs (L2570-2574)
```rust
        let validated_operation =
            validate_execute_extension_operation(self, execute_extension_operation).await?;

        // Execute the validated operation
        validated_operation.execute(self).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L444-446)
```rust
    /// Per-account lock for update_balance
    pub update_balance_accounts: BTreeSet<Account>,

```
