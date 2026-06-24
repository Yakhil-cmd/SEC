### Title
SNS Treasury Deposit 50% Cap Bypassed via Multiple Concurrent Proposals - (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

The `validate_deposit_operation_impl` function in SNS governance checks only whether a **single** deposit proposal's requested amount exceeds 50% of the current treasury balance. It does not account for other pending (adopted-but-not-yet-executed) deposit proposals, nor does it re-validate the cap at execution time. An SNS token holder coalition with sufficient voting power can submit multiple proposals each requesting just under 50%, all of which pass validation independently, and collectively drain far more than 50% of the treasury.

---

### Finding Description

`validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs` enforces a 50% cap on treasury deposits:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(...)
}
if icp_requested > icp_balance.checked_div(2).unwrap() {
    return Err(...)
}
```

This check fires only at **proposal submission/validation time** and only compares the single proposal's amount against the live balance. It does not:

1. Sum up amounts already committed in other pending `ExecuteExtensionOperation` deposit proposals.
2. Re-validate the cap at **execution time** (unlike `TransferSnsTreasuryFunds`, which calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` at execution).

The execution path `ValidatedExecuteExtensionOperation::execute` → `execute_treasury_manager_deposit` proceeds directly to `approve_treasury_manager` with no re-check: [1](#0-0) [2](#0-1) 

Contrast with `TransferSnsTreasuryFunds`, which explicitly re-checks the 7-day cumulative total at execution time: [3](#0-2) 

The flawed single-proposal-only check: [4](#0-3) 

---

### Impact Explanation

An SNS token holder coalition controlling enough voting power to pass `TreasuryAssetManagement` proposals can drain well beyond 50% of the SNS or ICP treasury into a treasury manager extension canister in a single governance cycle. The 50% cap — the only rate-limiting safeguard for `ExecuteExtensionOperation` deposits — is rendered ineffective. Once funds are inside the extension canister, the extension canister's own logic (outside SNS governance control) governs their disposition.

---

### Likelihood Explanation

Any SNS with a treasury manager extension registered is exposed. The attack requires only that the attacker controls enough voting power to pass `TreasuryAssetManagement`-topic proposals (which are `Critical` criticality). A coordinated group of large token holders, or a single whale, can submit two proposals in the same governance round before either executes. SNS governance proposals are publicly visible and the voting window is finite, making this a realistic scenario for any SNS with a large treasury and concentrated token distribution.

---

### Recommendation

1. **At validation time**: Sum the `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` across all currently open (not yet executed/failed) `ExecuteExtensionOperation` deposit proposals and include that total when checking against the 50% cap — mirroring how `total_treasury_transfer_amount_tokens` is used for `TransferSnsTreasuryFunds`.

2. **At execution time**: Re-validate the 50% cap inside `execute_treasury_manager_deposit` against the live balance at the moment of execution, analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. [5](#0-4) 

---

### Proof of Concept

**Setup**: SNS treasury holds 1,000 SNS tokens and 1,000 ICP. The 50% cap means at most 500 of each should be depositable at once.

**Attack**:
1. Attacker submits **Proposal A**: `treasury_allocation_sns_e8s = 490 SNS` (49% < 50% → passes `validate_deposit_operation_impl`).
2. Before Proposal A executes, attacker submits **Proposal B**: `treasury_allocation_sns_e8s = 490 SNS`. The treasury balance is still 1,000 SNS (Proposal A hasn't executed yet), so 490 < 500 → **also passes**.
3. Both proposals are adopted and executed sequentially. `execute_treasury_manager_deposit` calls `approve_treasury_manager` for each with no re-check.
4. **Result**: 980 SNS (98% of treasury) is transferred to the extension canister — nearly double the intended 50% cap.

The root cause is directly in `validate_deposit_operation_impl` at lines 308–318, which checks only `sns_requested > sns_balance / 2` for the current proposal in isolation, with no awareness of concurrent pending proposals and no execution-time re-validation. [6](#0-5)

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

**File:** rs/sns/governance/src/extensions.rs (L604-620)
```rust
impl ValidatedExecuteExtensionOperation {
    pub async fn execute(self, governance: &Governance) -> Result<(), GovernanceError> {
        let Self {
            operation_name: _,
            extension_canister_id,
            arg,
        } = self;

        match arg {
            ValidatedOperationArg::TreasuryManagerDeposit(arg) => {
                execute_treasury_manager_deposit(governance, extension_canister_id, arg).await
            }
            ValidatedOperationArg::TreasuryManagerWithdraw(arg) => {
                execute_treasury_manager_withdraw(governance, extension_canister_id, arg).await
            }
        }
    }
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1573)
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
```

**File:** rs/sns/governance/src/governance.rs (L2999-3005)
```rust

        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2658)
```rust
pub(crate) fn transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err<'a>(
    transfer: &TransferSnsTreasuryFunds,
    valuation: Valuation,
    proposals: impl Iterator<Item = &'a ProposalData>,
    now_timestamp_seconds: u64,
) -> Result<(), GovernanceError> {
    let allowance_tokens = transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)
        .map_err(|err| {
            // This should not be possible, because valuation was already used the same way during
            // proposal submission/creation/validation.
            GovernanceError::new_with_message(
                ErrorType::InconsistentInternalData,
                format!(
                    "Unable to determined upper bound on the amount of \
                     TransferSnsTreasuryFunds proposals: {err:?}\nvaluation:{valuation:?}",
                ),
            )
        })?;

    // The total calculated here _could_ be different from what was calculated at proposal
    // submission/creation time. A difference would result from the execution of (another)
    // TransferSnsTreasuryFunds proposal between now and then.
    let spent_tokens = total_treasury_transfer_amount_tokens(
        proposals,
        transfer.from_treasury(),
        now_timestamp_seconds - 7 * ONE_DAY_SECONDS,
    )
    .map_err(|message| {
        GovernanceError::new_with_message(ErrorType::InconsistentInternalData, message)
    })?;

    let remainder_tokens = allowance_tokens - spent_tokens;
    let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)
        // This Err cannot be provoked, because we are dividing a u64 (amount_e8s) by a positive
        // integer (E8).
        .ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::UnreachableCode,
                format!(
                    "Unable to convert proposals amount {} e8s to tokens.",
                    transfer.amount_e8s,
                ),
            )
        })?;
    if transfer_amount_tokens > remainder_tokens {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Executing this proposal is not allowed at this time, because doing \
                 so would cause the 7 day upper bound of {allowance_tokens} tokens to be exceeded. \
                 Maybe, try again later? The total amount transferred in the past \
                 7 days stands at {spent_tokens} tokens, and the amount in this proposal is {transfer_amount_tokens} \
                 tokens. The upper bound is based on treasury valuation factors at \
                 the time of proposal submission: {valuation:?}",
            ),
        ));
    }

    Ok(())
```
