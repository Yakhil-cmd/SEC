### Title
Circular On-Chain Balance Check Used as Slippage Guard for SNS Treasury Deposits — (`File: rs/sns/governance/src/extensions.rs`)

---

### Summary

The `validate_deposit_operation_impl` function in SNS Governance checks that the requested deposit amounts do not exceed 50% of the **current on-chain treasury balance** at the time of proposal validation. This balance is read from the live ledger state at validation time, not from a fixed off-chain reference. The check is therefore circular: it is derived from the same mutable on-chain state that an adversary can influence. This is the direct IC analog of the M-05 finding: an on-chain-derived "expected amount" is used as the sole guard, making the protection ineffective when the underlying state has already been manipulated.

---

### Finding Description

In `rs/sns/governance/src/extensions.rs`, `validate_deposit_operation_impl` performs the following check at proposal validation time:

```rust
let sns_balance = governance.ledger.account_balance(...).await?;
let icp_balance = governance.nns_ledger.account_balance(...).await?;

if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(...);
}
if icp_requested > icp_balance.checked_div(2).unwrap() {
    return Err(...);
}
```

The 50%-of-current-balance guard is the **only** financial protection applied before `approve_treasury_manager` grants an ICRC-2 allowance and `execute_treasury_manager_deposit` calls the external DEX canister. The guard is computed entirely from the live ledger balance at the moment of validation, not from any value fixed at proposal creation time.

The `treasury_manager.did` interface itself acknowledges this class of risk in its preamble:

> *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."*

And the proposal rendering in `rs/sns/governance/src/proposal.rs` repeats the same warning without enforcing any mitigation at the protocol level.

The execution path is:

1. `validate_execute_extension_operation` → calls `validate_deposit_operation_impl` (reads live balance, computes 50% cap).
2. `ValidatedExecuteExtensionOperation::execute` → calls `execute_treasury_manager_deposit`.
3. `execute_treasury_manager_deposit` → calls `approve_treasury_manager` (grants ICRC-2 allowance for the full `treasury_allocation_*_e8s` amount), then calls `extension_canister.deposit(...)`.

There is **no re-validation of the balance at execution time**, and no minimum-LP-tokens-out or minimum-price parameter passed to the DEX. The validated amounts from proposal time are used directly.

---

### Impact Explanation

**Cycles/resource accounting bug / ledger conservation bug** — specifically a wrong minimum-amount check that provides no real protection when the on-chain state is manipulated.

1. **Circular guard**: The 50% cap is derived from the live treasury balance. If the treasury balance has been legitimately reduced (e.g., by a prior transfer proposal, or by a prior deposit that partially succeeded), the cap shrinks proportionally. The guard does not protect against the case where the DEX pool itself is imbalanced at execution time.

2. **No minimum output enforced**: The `DepositRequest` sent to the treasury manager canister carries only `allowances` (the amounts approved for spending). There is no `min_lp_tokens_out` or equivalent parameter. The treasury manager DID interface does not include such a field. This means the DEX can return arbitrarily few LP tokens and the governance canister has no way to detect or reject the outcome.

3. **Time-of-check vs. time-of-use gap**: SNS governance proposals have a voting period of days. The balance read during validation is stale by the time execution occurs. Any balance change between validation and execution is invisible to the guard.

**Concrete impact**: An SNS treasury deposit proposal can be executed at a moment when the DEX pool is severely imbalanced (e.g., 99% of one asset, 1% of the other), causing the SNS treasury to receive far fewer LP tokens than the deposited value warrants. The 50%-of-balance cap does not prevent this; it only limits the absolute size of the deposit, not the quality of the exchange rate received.

---

### Likelihood Explanation

- The SNS Treasury Manager extension is a new, production-targeted feature (the DID is marked as a draft but is in the production codebase).
- Any SNS that registers a TreasuryManager extension and passes a deposit proposal is exposed.
- The attacker does not need privileged access: they only need to observe the IC mempool (or predict the proposal execution block) and manipulate the DEX pool state before the governance canister's `execute` call lands. On IC, inter-canister calls are atomic within a round but cross-round manipulation is possible.
- The DID's own security-risk comment confirms the developers are aware of the class of issue but have not enforced a mitigation at the protocol level.

---

### Recommendation

1. **Pass a caller-supplied minimum LP token output** as a parameter in the `ExecuteExtensionOperation` argument (analogous to the M-05 recommendation of computing the minimum off-chain and passing it to the function). The `DepositRequest` type in `treasury_manager.did` should be extended with a `min_lp_tokens_out` field, and the governance canister should forward it to the treasury manager.

2. **Re-validate the balance at execution time** (not only at proposal validation time) and reject if the ratio of requested amounts to current balance has changed beyond a tolerance.

3. **Enforce a minimum price check** inside the treasury manager canister itself, using an on-chain price oracle (e.g., a TWAP from the DEX, or an external price feed), rather than relying solely on the governance-side 50% cap.

---

### Proof of Concept

**Step 1 — Proposal submission**: An SNS neuron submits an `ExecuteExtensionOperation` deposit proposal with `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y`, where both are ≤ 50% of the current treasury balance.

**Step 2 — Validation** (`validate_deposit_operation_impl`): The governance canister reads the live treasury balance and confirms X ≤ balance/2 and Y ≤ balance/2. The proposal passes validation and enters the voting period.

**Step 3 — Pool manipulation**: Before the proposal's voting period ends (or in the same round as execution), an adversary (or a natural market event) causes the DEX pool to become severely imbalanced — e.g., by depositing a large amount of one asset, driving the price of the other asset to near zero within the pool.

**Step 4 — Execution** (`execute_treasury_manager_deposit`): The governance canister calls `approve_treasury_manager` granting the full X SNS tokens and Y ICP as an ICRC-2 allowance, then calls `extension_canister.deposit(DepositRequest { allowances })`. The treasury manager forwards the tokens to the DEX. Because the pool is imbalanced, the DEX returns a tiny number of LP tokens. There is no `min_lp_tokens_out` check anywhere in the call chain.

**Step 5 — Loss**: The SNS treasury has permanently transferred X SNS tokens and Y ICP to the DEX in exchange for LP tokens worth a fraction of the deposited value. The governance canister logs success.

**Relevant code locations**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
