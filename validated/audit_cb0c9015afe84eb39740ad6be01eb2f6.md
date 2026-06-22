### Title
Missing Slippage Protection in SNS Treasury Manager DEX Deposit/Withdraw — (`rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager framework allows SNS DAOs to deposit treasury funds (SNS tokens + ICP) into external DEX liquidity pools (e.g., KongSwap) via a governance proposal. Neither the `DepositRequest` API nor the governance execution layer enforces any minimum LP-token output or slippage tolerance. A malicious actor can front-run or sandwich the deposit transaction between proposal approval and on-chain execution, causing the SNS treasury to receive significantly fewer LP tokens than expected for the deposited assets.

---

### Finding Description

The `DepositRequest` type defined in the Treasury Manager interface contains only `allowances` — the amounts of each asset to deposit — with no `min_lp_tokens_out` or equivalent slippage guard:

```
// rs/sns/treasury_manager/src/lib.rs
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

The governance execution function `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` approves a fixed token amount, calls `deposit` on the treasury manager canister, and only checks whether the call returned an error. It does **not** inspect the returned `Balances` to verify that the LP tokens received meet any minimum threshold:

```rust
// rs/sns/governance/src/extensions.rs ~L1576-1607
let balances = governance
    .env
    .call_canister(extension_canister_id, "deposit", arg_blob)
    ...
    .map_err(|err| { ... })?;

log!(INFO, "TreasuryManager.deposit succeeded with response: {:?}", balances);
Ok(())   // <-- no check on balances.external_custodian vs. expected minimum
```

The same pattern applies to `execute_treasury_manager_withdraw` (lines 1612–1660): the returned balances are decoded and logged but never validated against a minimum expected withdrawal amount.

The codebase itself acknowledges this gap. The DID file states:

> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."

And the proposal rendering in `validate_and_render_register_extension` includes a `## WARNING` block:

> "Some Decentralized Exchanges lack slippage protection during deposits … This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks."

The warning is informational only; no enforcement mechanism exists in the protocol code.

---

### Impact Explanation

An SNS DAO's treasury deposit proposal specifies exact token amounts (e.g., 100 ICP + 200 SNS tokens). Between proposal approval and execution — a window that can span hours to days due to governance voting delays — an attacker can:

1. Observe the pending proposal and the target KongSwap pool.
2. Manipulate the pool's price ratio by making large trades before the deposit executes.
3. The deposit executes at the manipulated ratio, giving the DAO far fewer LP tokens than the deposited assets are worth.
4. The attacker reverses their trades, extracting value at the DAO's expense.

On withdrawal, the DAO redeems LP tokens at the (now restored) market price, receiving fewer underlying tokens than originally deposited. The loss is permanent and comes directly from the SNS treasury — funds held on behalf of all SNS token holders.

---

### Likelihood Explanation

SNS governance proposals are public and have mandatory voting periods (typically 4 days). This gives any observer a large, predictable window to front-run the deposit. KongSwap is an AMM-style DEX where price ratios are directly manipulable by large trades. The KongSwap Adaptor is already integrated and tested in the IC monorepo, making this a live attack surface rather than a theoretical one.

---

### Recommendation

1. **Add a `min_lp_tokens_out` field to `DepositRequest`** (and a `min_tokens_out` to `WithdrawRequest`) so that the treasury manager canister can enforce slippage bounds at the DEX call site.
2. **Validate the returned `Balances`** in `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw` against the minimum values specified in the proposal, reverting (and refunding) if the actual output falls below the threshold.
3. **Require proposers to specify a slippage tolerance** as part of the `ExtensionOperationArg` for deposit/withdraw operations, and validate it in `validate_deposit_operation_impl`.

---

### Proof of Concept

**Step 1 — Proposal submitted.** An SNS DAO submits an `ExecuteExtensionOperation` proposal to deposit 100 ICP + 200 SNS tokens into KongSwap. The proposal enters a 4-day voting window.

**Step 2 — Front-run.** An attacker observes the proposal. Just before execution, they buy a large amount of SNS tokens from the KongSwap pool, skewing the SNS/ICP ratio heavily in favor of ICP (i.e., SNS becomes artificially cheap relative to ICP).

**Step 3 — Deposit executes without slippage check.** `execute_treasury_manager_deposit` calls `approve_treasury_manager` then `deposit`. The KongSwap adaptor deposits at the manipulated ratio. The DAO receives LP tokens representing a position worth significantly less than 100 ICP + 200 SNS.

**Step 4 — Attacker reverses.** The attacker sells their SNS tokens back, restoring the ratio and pocketing the spread.

**Step 5 — Withdrawal.** When the DAO later withdraws, it redeems LP tokens at fair market value — but the LP position was entered at an unfavorable ratio, so the DAO recovers fewer tokens than it deposited. The shortfall is a direct, irreversible loss from the SNS treasury.

No privileged access is required. The attacker only needs to monitor public SNS governance proposals and execute trades on KongSwap.

---

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

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

**File:** rs/sns/governance/src/extensions.rs (L1575-1610)
```rust
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

**File:** rs/sns/governance/src/extensions.rs (L1612-1660)
```rust
/// Execute a treasury manager withdraw operation
async fn execute_treasury_manager_withdraw(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedWithdrawOperationArg,
) -> Result<(), GovernanceError> {
    let arg_blob = construct_treasury_manager_withdraw_payload(arg.original).map_err(|err| {
        GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!("Failed to construct treasury manager withdraw payload: {err}"),
        )
    })?;

    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.withdraw failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!(
                        "Error decoding TreasuryManager.withdraw response: {err:?}"
                    ),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.withdraw failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.withdraw succeeded with response: {:?}",
        balances
    );

    Ok(())
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1551)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

## Extension Configuration

The extension will be deployed and configured according to the provided parameters.",
    ))
}
```
