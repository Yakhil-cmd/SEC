### Title
Missing Deadline Parameter in SNS Treasury Manager Deposit — Price Ratio at Execution May Differ from Proposal Approval Time - (File: rs/sns/governance/src/extensions.rs)

### Summary

The SNS Treasury Manager deposit flow (`execute_treasury_manager_deposit`) does not enforce any deadline between when a governance proposal is approved and when the actual deposit into the external custodian (e.g., a DEX liquidity pool) is executed. The `treasury_manager.did` file explicitly acknowledges this as a known security risk: *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."* No mechanism exists in the on-chain code to bound how stale the deposit operation can be.

### Finding Description

When an SNS DAO approves a `TreasuryManagerDeposit` proposal, the following sequence occurs:

1. **Proposal submission and validation** — `validate_deposit_operation_impl` checks that the requested amounts do not exceed 50% of the current treasury balance at proposal submission time.
2. **Voting period** — The proposal goes through an initial voting period (default 4 days for normal proposals, 5 days for critical ones) plus up to `2 × wait_for_quiet_deadline_increase_seconds` of extension.
3. **Execution** — `execute_treasury_manager_deposit` is called, which:
   - Calls `approve_treasury_manager` to issue ICRC-2 allowances expiring in **1 hour** from execution time.
   - Calls `deposit` on the Treasury Manager canister, which forwards funds to an external custodian (DEX).

The critical gap is between steps 1 and 3. The proposal can sit in the voting queue for days (4–10+ days with wait-for-quiet extensions). During this time, the SNS/ICP price ratio in the DEX pool can shift substantially. There is no deadline parameter in the `DepositRequest`, no check in `execute_treasury_manager_deposit` that the current price ratio is still acceptable relative to what was expected at proposal creation, and no slippage bound enforced by the protocol.

The 1-hour ICRC-2 allowance expiry set in `approve_treasury_manager` only limits how long the Treasury Manager canister has to pull the funds after governance approves — it does not protect against the price having moved during the voting period itself.

**Root cause code path:** [1](#0-0) 

The `execute_treasury_manager_deposit` function issues the allowance and immediately calls `deposit` with no staleness check: [2](#0-1) 

The allowance expiry is set to `now + ONE_HOUR_SECONDS` at execution time, not at proposal creation time: [3](#0-2) 

The `DepositRequest` carries no deadline or minimum-price-ratio field: [4](#0-3) 

The known risk is explicitly documented but unmitigated: [5](#0-4) 

Validation at proposal submission time checks balances but not price: [6](#0-5) 

### Impact Explanation

An SNS DAO approves a deposit proposal expecting a certain SNS/ICP price ratio in the DEX pool. After the voting period (potentially 4–10 days), the market price may have moved significantly. The deposit executes at the new, unfavorable ratio, causing the DAO treasury to receive far fewer LP tokens or pool shares than expected — a direct, quantifiable loss of treasury value. Since there is no slippage protection in the protocol layer, the full price impact is absorbed by the DAO. This is a governance-authorized but economically harmful execution.

**Impact: High** — direct loss of SNS DAO treasury funds with no protocol-level bound on the magnitude of loss.

### Likelihood Explanation

**Likelihood: Medium** — SNS governance proposals for treasury deposits are a normal, expected operation. Voting periods of 4–10 days are standard. DEX price ratios routinely move by 5–30%+ over such periods, especially for smaller SNS tokens. No attacker action is required; the loss occurs through normal protocol operation whenever market conditions shift during the voting window.

### Recommendation

1. **Add a `deadline_timestamp_seconds` field to `DepositRequest`** (and `TreasuryManagerInit`) so that the Treasury Manager canister can reject execution if the current time exceeds the deadline.
2. **Add a `min_price_ratio` or `min_lp_tokens_out` field** to `DepositRequest` so the Treasury Manager can enforce slippage bounds when interacting with the DEX.
3. **In `execute_treasury_manager_deposit`**, check that `env.now() <= proposal_creation_timestamp + max_acceptable_delay` before proceeding, reverting if the proposal is too stale.
4. **Set the ICRC-2 allowance expiry** relative to proposal creation time rather than execution time, so that a delayed execution causes the allowance to be expired and the deposit to fail safely.

### Proof of Concept

1. SNS DAO submits a `TreasuryManagerDeposit` proposal at time T₀ with SNS/ICP pool ratio R₀. The proposal requests depositing X SNS + Y ICP into a DEX pool.
2. The proposal enters the voting period (5 days for critical `TreasuryAssetManagement` topic) plus wait-for-quiet extensions — total up to ~10 days.
3. At time T₀ + 10 days, the SNS token price has dropped 40% relative to ICP. The pool ratio is now R₁ = 0.6 × R₀.
4. `execute_treasury_manager_deposit` is called. It calls `approve_treasury_manager` (allowance valid for 1 hour from now), then calls `deposit` on the Treasury Manager.
5. The Treasury Manager deposits X SNS + Y ICP into the DEX at ratio R₁. Because the SNS price is lower, the DAO receives significantly fewer LP tokens than it would have at R₀, and the excess ICP may be returned — but the SNS tokens are deposited at the unfavorable rate.
6. No check in `execute_treasury_manager_deposit`, `DepositRequest`, or the Treasury Manager API prevents this execution or bounds the loss.

The entry path is fully unprivileged from the attacker's perspective — no special role is needed. Any SNS DAO that uses the Treasury Manager extension is exposed to this risk on every deposit proposal.

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

**File:** rs/sns/governance/src/extensions.rs (L788-789)
```rust
        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```
