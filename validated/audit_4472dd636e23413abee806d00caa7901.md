### Title
SNS Treasury Deposit 50% Balance Check Does Not Account for Pending Approved Proposals - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The `validate_deposit_operation_impl` function enforces a 50% treasury balance limit on deposit proposals at **submission time only**. Because the check is not re-performed at execution time, and because it does not account for other already-approved-but-not-yet-executed deposit proposals, multiple proposals can collectively commit well over 50% of the SNS treasury to the treasury manager — directly analogous to the reported pattern of treating already-allocated funds as available liquidity.

### Finding Description

`validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs` queries the live ledger balance and rejects any deposit proposal that requests more than 50% of the current treasury: [1](#0-0) 

This check is invoked only during proposal validation (submission time), via `ExtensionOperationSpec::validate_operation_arg` → `validate_deposit_operation` → `validate_deposit_operation_impl`.

The execution path is entirely separate. `ValidatedExecuteExtensionOperation::execute` calls `execute_treasury_manager_deposit`, which proceeds directly to `approve_treasury_manager` without re-invoking the 50% check: [2](#0-1) 

`approve_treasury_manager` sets an ICRC-2 allowance for the exact amount encoded in the proposal at submission time, with no reference to the current treasury balance: [3](#0-2) 

**Root cause**: The 50% limit is a snapshot check against the ledger balance at proposal submission time. It does not track or subtract amounts already committed by other open (approved, not yet executed) deposit proposals. The treasury balance seen by the check is the **gross** balance, not the **net available** balance after accounting for pending commitments — exactly the same class of error as the reported `rewardsByToken[pos.buyToken].totalRewards` misuse.

**Attack scenario**:
1. Neuron holder submits Proposal A: deposit 49% of treasury (e.g., 490 of 1000 tokens). Passes the 50% check (490 ≤ 500). Passes voting.
2. Before A executes, the same or another neuron holder submits Proposal B: deposit 49% of treasury (490 of 1000 tokens). Passes the 50% check (treasury still shows 1000 tokens). Passes voting.
3. Proposal A executes: allowance of 490 tokens set; treasury manager pulls 490 tokens. Treasury now holds 510 tokens.
4. Proposal B executes: allowance of 490 tokens set (no re-check); treasury manager pulls 490 tokens from the remaining 510. Treasury is left with only 20 tokens — 98% of the treasury has been committed, far exceeding the intended 50% cap.

### Impact Explanation

The 50% per-proposal limit is the primary safety guardrail preventing excessive treasury commitment to an external treasury manager canister. Bypassing it allows the SNS treasury to be nearly fully drained into the treasury manager in a single governance cycle. While the treasury manager is a trusted canister, the funds are no longer under direct SNS governance control, and any bug or exploit in the treasury manager (e.g., a DEX interaction gone wrong) would affect the full treasury rather than the intended 50% maximum. **Impact: Medium** — funds are not immediately lost but are over-committed beyond the intended safety boundary.

### Likelihood Explanation

Any SNS neuron holder can submit `ExecuteExtensionOperation` proposals. Two proposals can be submitted in the same governance period and both pass voting independently. No coordination beyond normal proposal submission is required. The scenario is reachable through ordinary governance participation without requiring a malicious supermajority — two independently well-intentioned proposals submitted close together would trigger the same outcome. **Likelihood: Medium**.

### Recommendation

Re-perform the 50% balance check at execution time inside `execute_treasury_manager_deposit`, querying the live ledger balance immediately before calling `approve_treasury_manager`. Additionally, track the total amount committed by all currently open (approved, not yet executed) deposit proposals and subtract that from the available balance before applying the 50% check at submission time, so that the check reflects the **net uncommitted** treasury balance rather than the gross balance.

### Proof of Concept

1. Deploy an SNS with a treasury holding 1,000 SNS tokens and a registered treasury manager extension.
2. Submit Proposal A (`ExecuteExtensionOperation`, `deposit`, `treasury_allocation_sns_e8s = 490_000_000`). Validation passes: 490 ≤ 500 (50% of 1000).
3. Before Proposal A is executed, submit Proposal B with identical parameters. Validation passes again: the ledger still shows 1000 tokens; no pending-proposal accounting exists.
4. Pass both proposals through voting.
5. Execute Proposal A: `approve_treasury_manager` sets allowance of 490 tokens; treasury manager pulls 490 tokens. Treasury balance: 510 tokens.
6. Execute Proposal B: `approve_treasury_manager` sets allowance of 490 tokens (no re-check of current balance at `rs/sns/governance/src/extensions.rs:1567-1573`); treasury manager pulls 490 tokens from the remaining 510. Treasury balance: 20 tokens.
7. Observe that 980 of 1000 tokens (98%) have been committed to the treasury manager, violating the 50% safety limit enforced only at submission time. [4](#0-3) [5](#0-4)

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

**File:** rs/sns/governance/src/extensions.rs (L777-830)
```rust
    async fn approve_treasury_manager(
        &self,
        treasury_manager_canister_id: CanisterId,
        sns_amount_e8s: u64,
        icp_amount_e8s: u64,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: treasury_manager_canister_id.get().0,
            subaccount: None,
        };

        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);

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
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making SNS Token treasury transfer: {e}"),
                )
            })?;

        self.nns_ledger
            .icrc2_approve(
                to,
                icp_amount_e8s,
                Some(expiry_time_nsec),
                icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                self.icp_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making ICP Token treasury transfer: {e}"),
                )
            })?;

        Ok(())
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
