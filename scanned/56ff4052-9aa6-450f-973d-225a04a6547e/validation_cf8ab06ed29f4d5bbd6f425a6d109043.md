### Title
Lack of Slippage Protection in SNS Treasury Manager Deposit Flow - (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager deposit execution path (`execute_treasury_manager_deposit`) approves ICRC-2 allowances for SNS and ICP tokens and calls `deposit` on a DEX-backed treasury manager canister without any mechanism to enforce a minimum output (LP tokens received). The `DepositRequest` API type contains no slippage tolerance field, and the governance execution code does not verify the value received in return. This is explicitly acknowledged as a "Known Security Risk" in `rs/sns/treasury_manager/treasury_manager.did` but remains unmitigated at the protocol level.

---

### Finding Description

When an SNS DAO adopts a `TreasuryManagerDeposit` or `RegisterExtension` proposal, the governance canister executes `execute_treasury_manager_deposit`: [1](#0-0) 

The function:
1. Calls `approve_treasury_manager` to grant ICRC-2 allowances to the treasury manager canister for both SNS tokens and ICP.
2. Calls `deposit` on the treasury manager with those allowances.
3. Logs the response but **does not verify the amount of LP tokens or value received**. [2](#0-1) 

The `DepositRequest` type in the Treasury Manager API contains only `allowances` (input amounts) — there is no field for minimum expected output, minimum LP tokens, or price ratio: [3](#0-2) 

The only pre-execution check is `validate_deposit_operation_impl`, which verifies that the requested amounts do not exceed 50% of the current treasury balance at proposal validation time. It does not enforce any minimum output at execution time: [4](#0-3) 

The `treasury_manager.did` specification itself explicitly acknowledges this as an unresolved risk: [5](#0-4) 

The `approve_treasury_manager` function grants a 1-hour expiry ICRC-2 allowance, creating a window during which the DEX pool can be manipulated: [6](#0-5) 

---

### Impact Explanation

An attacker who observes a pending SNS governance proposal to deposit treasury funds into a DEX (all proposals are public on-chain) can manipulate the DEX pool price before the proposal executes. Because the governance code approves a fixed token allowance and calls `deposit` with no minimum output constraint, the treasury manager will accept whatever LP tokens the DEX returns — even a near-zero amount. The attacker profits from the price manipulation while the SNS treasury suffers a permanent loss of value. The `Balances` response returned by `deposit` is only logged, not validated against any expected minimum: [7](#0-6) 

---

### Likelihood Explanation

SNS governance proposals are public and have a multi-day voting period followed by execution. This gives any observer a large window to observe the pending deposit and pre-position in the DEX pool. The attacker needs only sufficient capital to move the DEX pool price, which is feasible for any well-capitalized actor targeting a specific SNS treasury deposit. The `treasury_manager.did` itself acknowledges this risk as realistic ("the price ratio at the time of execution may differ from the ratio at the time the proposal was approved"). [5](#0-4) 

---

### Recommendation

1. Add a `min_lp_tokens_out` or `min_price_ratio` field to the `DepositRequest` type in `treasury_manager.did` so that treasury manager implementations can enforce a minimum output.
2. In `execute_treasury_manager_deposit`, require the `DepositRequest` to include minimum output parameters derived from the price at proposal adoption time, and revert if the actual output reported in `Balances` falls below the threshold.
3. Alternatively, enforce at the governance level that the `Balances` response from `deposit` is validated against the expected minimum before the proposal is marked as successfully executed.

---

### Proof of Concept

1. An SNS DAO submits and adopts a `TreasuryManagerDeposit` proposal to deposit `X` SNS tokens and `Y` ICP into a DEX liquidity pool via a registered treasury manager.
2. The proposal is public. An attacker observes it during the voting period.
3. Before the proposal executes, the attacker swaps a large amount of one token in the DEX pool, severely imbalancing the reserves and moving the price against the SNS treasury's deposit ratio.
4. The governance canister executes `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` grants ICRC-2 allowances for `X` SNS and `Y` ICP to the treasury manager.
   - `deposit` is called; the treasury manager deposits into the manipulated DEX pool and receives far fewer LP tokens than the fair-price equivalent.
   - The `Balances` response is logged but not validated.
5. The attacker reverses their position in the DEX pool, extracting the value lost by the SNS treasury.
6. The SNS treasury has permanently lost value with no on-chain recourse, as the governance proposal is marked executed successfully. [8](#0-7) [9](#0-8)

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

**File:** rs/sns/governance/src/extensions.rs (L777-831)
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-93)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};

type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
};
```
