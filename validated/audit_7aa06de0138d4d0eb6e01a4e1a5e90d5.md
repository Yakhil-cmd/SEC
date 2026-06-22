### Title
No Slippage Protection on SNS Treasury Manager DEX Deposits Enables Sandwich Attacks - (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS Treasury Manager extension framework allows SNS DAOs to deposit treasury assets (SNS tokens + ICP) into external DEX liquidity pools via governance proposals. The deposit flow enforces no slippage bound or minimum price-ratio check at execution time. Because governance proposals are public and have multi-day voting periods, an attacker can observe a pending deposit proposal and sandwich the SNS treasury's deposit — manipulating the DEX pool price before execution and reversing after — causing the SNS treasury to deposit at an unfavorable ratio and suffer a direct loss of funds.

---

### Finding Description

The deposit flow is split across two files:

**Validation (`validate_deposit_operation_impl`)** in `rs/sns/governance/src/extensions.rs` checks only that the requested amounts do not exceed 50% of the current treasury balance:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(...)
}
if icp_requested > icp_balance.checked_div(2).unwrap() {
    return Err(...)
}
```

No price ratio, minimum LP tokens out, or slippage tolerance is validated. [1](#0-0) 

**Execution (`execute_treasury_manager_deposit`)** approves the treasury manager and then calls `deposit` on the extension canister, which forwards the assets to the DEX at whatever price the pool holds at that moment:

```rust
governance.approve_treasury_manager(...).await?;
governance.env.call_canister(extension_canister_id, "deposit", arg_blob).await...
``` [2](#0-1) 

The `DepositRequest` type in the Treasury Manager interface carries only `allowances` (asset + amount), with no slippage or minimum-output fields: [3](#0-2) 

The codebase itself acknowledges this gap explicitly:

> *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."* [4](#0-3) 

The same risk is surfaced as a UI-only warning in the proposal rendering for `RegisterExtension`, but no enforcement exists: [5](#0-4) 

---

### Impact Explanation

An attacker who observes a pending `ExecuteExtensionOperation` deposit proposal can:

1. **Before proposal execution**: Swap a large amount into the DEX pool to skew the SNS/ICP price ratio significantly.
2. **Proposal executes**: The SNS treasury deposits at the manipulated ratio, receiving far fewer LP tokens than expected.
3. **After execution**: The attacker reverses their swap, extracting the price impact as profit.

The SNS treasury (and all its token holders) suffer a direct, permanent loss of value. The attacker profits proportionally to the size of the treasury deposit and the depth of the pool. Because governance voting periods are days long, the attacker has ample time to prepare and execute the manipulation with no time pressure.

---

### Likelihood Explanation

- SNS governance proposals are fully public and observable by anyone on-chain.
- Voting periods are typically several days, giving attackers a large window.
- Any canister or user holding sufficient tokens on the target DEX can execute the manipulation.
- No privileged access, key compromise, or subnet-majority corruption is required.
- The IC's deterministic message ordering does not prevent this attack because the manipulation happens across separate governance rounds, not within a single block.

---

### Recommendation

1. **Add a `min_lp_tokens_out` or `max_price_impact_bps` field to `DepositRequest`** in `treasury_manager.did`, so the Treasury Manager implementation can enforce a slippage bound when calling the DEX.
2. **Enforce slippage at the governance layer**: `validate_deposit_operation_impl` should require the proposal to include a minimum acceptable price ratio, checked again at execution time against a DEX query.
3. **Reduce the execution window**: Consider a time-lock between proposal adoption and execution that is short enough to limit the attacker's manipulation window, or require a fresh price oracle check immediately before the DEX call.

---

### Proof of Concept

1. SNS DAO submits `ExecuteExtensionOperation { operation_name: "deposit", operation_arg: { treasury_allocation_sns_e8s: 10_000_000_000, treasury_allocation_icp_e8s: 5_000_000_000 } }`.
2. Proposal enters voting period (publicly visible on-chain).
3. Attacker calls the DEX directly, swapping a large ICP amount for SNS tokens, pushing the SNS/ICP price up by 30%.
4. Proposal passes and `execute_treasury_manager_deposit` runs: `approve_treasury_manager` grants allowances, then `call_canister(..., "deposit", ...)` deposits at the manipulated 30%-inflated SNS price.
5. The SNS treasury receives ~23% fewer LP tokens than it would have at the fair price.
6. Attacker reverses their swap, extracting the price impact as profit at the treasury's expense.

The root cause — absence of any slippage parameter in `DepositRequest` and absence of any price-ratio check in `validate_deposit_operation_impl` — is confirmed at: [1](#0-0) [3](#0-2)

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

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
