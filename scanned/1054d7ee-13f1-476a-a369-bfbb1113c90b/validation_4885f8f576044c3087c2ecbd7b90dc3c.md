### Title
Stale-Snapshot 50% Guard Bypass via Two Concurrent TreasuryManagerDeposit Proposals — (`rs/sns/governance/src/extensions.rs`)

---

### Summary

`validate_deposit_operation_impl` snapshots the treasury balance at proposal-validation time and enforces a ≤ 50% cap. However, `execute_treasury_manager_deposit` performs **no re-validation** at execution time, and there is **no concurrency guard** preventing two adopted deposit proposals from executing back-to-back. Two proposals each requesting ~49% of the treasury can both pass validation against the same snapshot, then execute sequentially, granting and consuming two separate 0.49B allowances and draining ~98% of the treasury.

---

### Finding Description

**Validation path — stale snapshot**

`validate_deposit_operation_impl` fetches the live balance once and checks `requested ≤ balance / 2`: [1](#0-0) 

The validated amounts are stored in `ValidatedDepositOperationArg` and carried forward to execution unchanged. There is no second balance check at execution time.

**Execution path — no re-validation, no guard**

`execute_treasury_manager_deposit` directly calls `approve_treasury_manager` with the stale validated amounts, then calls `deposit` on the extension canister: [2](#0-1) 

`approve_treasury_manager` issues `icrc2_approve` with `expected_allowance: None`, which blindly overwrites any existing allowance: [3](#0-2) 

The inline comment ("Therefore, there is no risk of double spending") is **incorrect** for the sequential-execution scenario. It is only true if the second `approve` fires before the first `transfer_from`. When each proposal's full `approve → deposit → transfer_from` cycle completes before the next proposal starts, both allowances are consumed independently.

**Proposals are spawned concurrently in the background**

`start_proposal_execution` uses `spawn_in_canister_env`, meaning multiple adopted proposals can be in flight simultaneously, interleaved at every `await` point: [4](#0-3) 

`process_proposals` iterates all open proposals and calls `process_proposal` for each in a single heartbeat, so two adopted deposit proposals are spawned in the same tick: [5](#0-4) 

**Contrast with `TransferSnsTreasuryFunds`**

The older `perform_transfer_sns_treasury_funds` explicitly guards against this with a thread-local `IN_PROGRESS_PROPOSAL_ID` lock and re-validates the 7-day spending cap at execution time: [6](#0-5) 

No equivalent guard exists for `execute_treasury_manager_deposit`.

---

### Impact Explanation

With treasury balance B:

| Step | Actor | Effect |
|---|---|---|
| Proposal-1 validated | Governance | Snapshot: balance=B, 0.49B ≤ B/2 ✓ |
| Proposal-2 validated | Governance | Snapshot: balance=B, 0.49B ≤ B/2 ✓ |
| Proposal-1 executes | Governance | `approve(0.49B)` → `deposit()` → extension `transfer_from(0.49B)` |
| Proposal-2 executes | Governance | `approve(0.49B)` → `deposit()` → extension `transfer_from(0.49B)` |
| **Net** | | **~0.98B drained ≈ 98% of treasury** |

The 50% invariant — the only hard cap protecting the treasury from a single deposit operation — is completely bypassed.

---

### Likelihood Explanation

- Requires two `ExecuteExtensionOperation / TreasuryManagerDeposit` proposals targeting the same extension canister to both be adopted by governance. This does not require a malicious majority; governance participants may legitimately vote yes on each proposal individually, believing each is safe (each is under 50%), without realising the combined effect.
- The extension canister calling `icrc2_transfer_from` immediately upon receiving `deposit` is the expected, correct behaviour per the `TreasuryManager` trait contract.
- No special privileges, key compromise, or subnet-majority attack is required.
- The missing guard is directly visible by comparing `execute_treasury_manager_deposit` with `perform_transfer_sns_treasury_funds`.

---

### Recommendation

1. **Re-validate at execution time**: Before calling `approve_treasury_manager`, re-fetch the live treasury balance and verify that `requested ≤ balance / 2`, accounting for any already-approved-but-unconsumed allowances.
2. **Add a concurrency guard**: Mirror the `IN_PROGRESS_PROPOSAL_ID` pattern from `perform_transfer_sns_treasury_funds` — reject a second deposit execution if one is already in flight for the same extension canister.
3. **Track outstanding allowances**: Maintain a per-extension-canister record of approved-but-not-yet-consumed allowances and include them in the 50% check at both validation and execution time.
4. **Fix the misleading comment** at line 791–792: `expected_allowance: None` does not prevent double-spending across sequential executions.

---

### Proof of Concept

```
State: treasury balance B = 1_000_000_000 e8s (1B)

1. Submit Proposal-1: TreasuryManagerDeposit(extension=X, sns=490_000_000)
   validate_deposit_operation_impl: balance=1B, 490M ≤ 500M ✓ → adopted

2. Submit Proposal-2: TreasuryManagerDeposit(extension=X, sns=490_000_000)
   validate_deposit_operation_impl: balance=1B (unchanged), 490M ≤ 500M ✓ → adopted

3. process_proposals() heartbeat:
   spawn execute_treasury_manager_deposit(Proposal-1)
   spawn execute_treasury_manager_deposit(Proposal-2)

4. Proposal-1 execution completes:
   icrc2_approve(spender=X, amount=490M, expected=None)
   call X.deposit(...)
   X calls icrc2_transfer_from(from=treasury, to=X, amount=490M) → block 1
   Allowance on ledger: 0

5. Proposal-2 execution completes:
   icrc2_approve(spender=X, amount=490M, expected=None)  ← new allowance, no conflict
   call X.deposit(...)
   X calls icrc2_transfer_from(from=treasury, to=X, amount=490M) → block 2
   Allowance on ledger: 0

Assert: total transferred = 980_000_000 e8s = 98% of B
Invariant violated: total deposited MUST NOT exceed 50% of initial balance.
```

A state-machine test with a mock extension canister that immediately calls `icrc2_transfer_from` in its `deposit` handler, and two concurrent proposals, would reproduce this deterministically.

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

**File:** rs/sns/governance/src/extensions.rs (L791-802)
```rust
        // If expected_allowance is None, the ledger *blindly* overwrites any existing
        // allowance (even if non-zero). Therefore, there is no risk of double spending.

        self.ledger
            .icrc2_approve(
                to,
                sns_amount_e8s,
                Some(expiry_time_nsec),
                self.transaction_fee_e8s_or_panic(),
                self.sns_treasury_subaccount(),
                None,
            )
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

**File:** rs/sns/governance/src/governance.rs (L2006-2025)
```rust
    /// Processes all proposals with decision status ProposalStatusOpen
    pub fn process_proposals(&mut self) {
        if self.env.now() < self.closest_proposal_deadline_timestamp_seconds {
            // Nothing to do.
            return;
        }

        let pids = self
            .proto
            .proposals
            .iter()
            .filter(|(_, info)| {
                info.status() == ProposalDecisionStatus::Open || info.accepts_vote(self.env.now())
            })
            .map(|(pid, _)| *pid)
            .collect::<Vec<u64>>();

        for pid in pids {
            self.process_proposal(pid);
        }
```

**File:** rs/sns/governance/src/governance.rs (L2118-2133)
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
```

**File:** rs/sns/governance/src/governance.rs (L2986-3005)
```rust
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

        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```
