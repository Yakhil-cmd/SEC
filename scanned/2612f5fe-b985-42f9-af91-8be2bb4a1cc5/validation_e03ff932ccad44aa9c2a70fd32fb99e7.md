### Title
Missing Reentrancy Guard in SNS Governance Treasury Manager Deposit/Withdraw Allows Concurrent Proposal Interleaving to Corrupt Allowance State - (`rs/sns/governance/src/extensions.rs`)

---

### Summary

The `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw` functions in SNS Governance perform multi-step async inter-canister calls (approve → deposit/withdraw) without any reentrancy guard. Two concurrently executing `ExecuteExtensionOperation` proposals can interleave across await points, causing the ICRC-2 allowance set by one proposal to be overwritten by another before the first `deposit` call consumes it, leading to incorrect treasury fund accounting or double-spend of the allowance.

---

### Finding Description

`perform_transfer_sns_treasury_funds` in `rs/sns/governance/src/governance.rs` explicitly acquires a per-proposal-type lock before making any inter-canister call:

```rust
let release_on_drop = acquire(&IN_PROGRESS_PROPOSAL_ID, proposal_id);
if let Err(already_in_progress_proposal_id) = release_on_drop {
    return Err(...);
}
``` [1](#0-0) 

The analogous `perform_execute_extension_operation` acquires **no such lock**:

```rust
async fn perform_execute_extension_operation(
    &self,
    execute_extension_operation: ExecuteExtensionOperation,
) -> Result<(), GovernanceError> {
    ...
    let validated_operation =
        validate_execute_extension_operation(self, execute_extension_operation).await?;
    validated_operation.execute(self).await?;
    Ok(())
}
``` [2](#0-1) 

`execute_treasury_manager_deposit` performs three sequential inter-canister calls with no guard:

1. `treasury_manager_deposit_context().await` — calls the SNS ledger for token symbol
2. `approve_treasury_manager(...)` — calls `icrc2_approve` on the SNS ledger, then `icrc2_approve` on the ICP ledger
3. `call_canister(extension_canister_id, "deposit", ...)` — calls the treasury manager [3](#0-2) 

The `approve_treasury_manager` function issues two sequential `icrc2_approve` calls: [4](#0-3) 

Each `.await` is a message boundary where the IC scheduler can interleave another proposal execution. Proposals are spawned in the background via `start_proposal_execution`: [5](#0-4) 

---

### Impact Explanation

Two adopted `ExecuteExtensionOperation` deposit proposals execute concurrently. Proposal A sets an ICRC-2 allowance of `X` SNS tokens for the treasury manager. Before Proposal A's `deposit` call fires, Proposal B's `approve_treasury_manager` overwrites the allowance with its own `X` SNS tokens. Now both proposals call `deposit` on the treasury manager canister. The treasury manager sees only one allowance of `X` but two `deposit` calls arrive. Depending on the treasury manager implementation, this can result in:

- The first `deposit` consuming the allowance; the second `deposit` fails silently or partially, leaving the SNS treasury in an inconsistent state.
- A concurrent deposit + withdraw interleaving where the withdraw executes against treasury state that is mid-deposit, causing incorrect balance accounting.

The SNS treasury holds real ICP and SNS tokens on behalf of token holders. Incorrect accounting constitutes a ledger conservation bug with direct financial impact. [6](#0-5) 

---

### Likelihood Explanation

**Low.** Requires two `ExecuteExtensionOperation` proposals to be adopted and begin execution within the same round or adjacent rounds. SNS governance proposals require a voting period, making simultaneous adoption of two such proposals uncommon but not impossible (e.g., a batch of proposals adopted at the same time via neuron following). The IC scheduler does interleave spawned futures across message boundaries, making the race condition real once two proposals are in flight.

---

### Recommendation

Add a per-operation-type reentrancy guard to `perform_execute_extension_operation`, mirroring the pattern used in `perform_transfer_sns_treasury_funds`:

```rust
thread_local! {
    static IN_PROGRESS_EXTENSION_OP: RefCell<Option<u64>> = const { RefCell::new(None) };
}
let release_on_drop = acquire(&IN_PROGRESS_EXTENSION_OP, proposal_id);
if let Err(in_progress_id) = release_on_drop {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        format!("Another ExecuteExtensionOperation proposal ({in_progress_id}) is already in progress."),
    ));
}
``` [7](#0-6) 

Alternatively, apply the guard inside `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw` directly.

---

### Proof of Concept

1. SNS governance has a registered treasury manager extension canister.
2. Two `ExecuteExtensionOperation` deposit proposals (Proposal A and Proposal B) are adopted simultaneously (e.g., via neuron following).
3. Both are spawned via `start_proposal_execution` → `perform_action` → `perform_execute_extension_operation`.
4. Proposal A enters `execute_treasury_manager_deposit` and calls `approve_treasury_manager`, issuing `icrc2_approve(treasury_manager, X_sns)` on the SNS ledger. This is an `.await` point.
5. Before Proposal A's SNS approval completes, Proposal B also enters `execute_treasury_manager_deposit` and calls `approve_treasury_manager`, issuing `icrc2_approve(treasury_manager, X_sns)` — overwriting Proposal A's pending allowance.
6. Both proposals proceed to call `call_canister(extension_canister_id, "deposit", ...)`.
7. The treasury manager receives two `deposit` calls but only one allowance of `X_sns` exists. The second `deposit` call either fails (leaving the treasury in a partially-executed state with funds already approved but not deposited) or the treasury manager's internal state becomes inconsistent.
8. The SNS treasury balance is now incorrect relative to what governance recorded as executed. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2118-2134)
```rust
    fn start_proposal_execution(&mut self, proposal_id: u64, action: Action) {
        // `perform_action` is an async method of &mut self.
        //
        // Starting it and letting it run in the background requires knowing that
        // the `self` reference will last until the future has completed.
        //
        // The compiler cannot know that, but this is actually true:
        //
        // - in unit tests, all futures are immediately ready, because no real async
        //   call is made. In this case, the transmutation to a static ref is abusive,
        //   but it's still ok since the future will immediately resolve.
        //
        // - in prod, "self" is a reference to the GOVERNANCE static variable, which is
        //   initialized only once (in canister_init or canister_post_upgrade)
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
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

**File:** rs/sns/governance/src/governance.rs (L2980-2998)
```rust
    async fn perform_transfer_sns_treasury_funds(
        &mut self,
        proposal_id: u64, // This is just to control concurrency.
        valuation: Result<Valuation, GovernanceError>,
        transfer: &TransferSnsTreasuryFunds,
    ) -> Result<(), GovernanceError> {
        // Only execute one proposal of this type at a time.
        thread_local! {
            static IN_PROGRESS_PROPOSAL_ID: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = acquire(&IN_PROGRESS_PROPOSAL_ID, proposal_id);
        if let Err(already_in_progress_proposal_id) = release_on_drop {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Another TransferSnsTreasuryFunds proposal (ID = {already_in_progress_proposal_id}) is already in progress.",
                ),
            ));
        }
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1610)
```rust
/// Execute a treasury manager deposit operation
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
