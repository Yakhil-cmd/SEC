### Title
Missing Slippage Protection in SNS Treasury Manager DEX Deposit — (`rs/sns/governance/src/extensions.rs`)

### Summary
The SNS Treasury Manager deposit flow approves and transfers SNS tokens and ICP into an external DEX liquidity pool with no minimum LP-token output enforced. Because SNS governance proposals are publicly visible for days before execution, an attacker can manipulate the DEX pool price during the voting window, causing the SNS treasury to receive fewer LP tokens than expected at proposal creation time.

### Finding Description
`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` performs two steps: it calls `approve_treasury_manager` to grant the treasury manager canister an ICRC-2 allowance for `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`, then calls `deposit` on the treasury manager canister. [1](#0-0) 

The `DepositRequest` type passed to `deposit` contains only `allowances` — the amounts to deposit — with no `minimum_lp_tokens_out` or any slippage-protection field: [2](#0-1) 

The upstream validation function `validate_deposit_operation_impl` only checks that the requested amounts do not exceed 50% of the current treasury balance. It performs no price-ratio check and enforces no minimum output: [3](#0-2) 

The `treasury_manager.did` file itself explicitly acknowledges this gap as a "Known Security Risk": [4](#0-3) 

The proposal-rendering code in `rs/sns/governance/src/proposal.rs` repeats the same warning, noting that "deposited asset ratios may deviate from those specified in the proposal" and that this "can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks": [5](#0-4) 

The mitigation cited — "any undeposited tokens are automatically returned to the SNS treasury account" — only covers the case where the DEX rejects the deposit entirely. It does **not** protect against the case where the DEX accepts the deposit at a worse-than-expected price ratio, silently issuing fewer LP tokens to the treasury.

### Impact Explanation
An attacker who observes a pending SNS governance proposal to deposit treasury assets into a DEX can trade against the pool during the multi-day voting period to shift the pool price. When the proposal executes, the SNS treasury deposits the full approved amounts but receives fewer LP tokens than the ratio implied at proposal creation time. The attacker then reverses their trade, extracting value from the SNS treasury. The loss scales linearly with the deposit size and the degree of price manipulation. Because the `DepositRequest` carries no minimum-output constraint, the treasury manager canister has no on-chain mechanism to abort the deposit when the realized price is unfavorable.

### Likelihood Explanation
SNS governance proposals are publicly visible on-chain for days before execution. Any participant who can trade on the target DEX pool can execute this attack. The attacker needs capital proportional to the pool depth to move the price meaningfully, but the profit opportunity scales with the deposit size. The attack requires no privileged access, no key compromise, and no consensus-level manipulation — only the ability to submit canister calls to the DEX during the voting window.

### Recommendation
1. Add a `minimum_lp_tokens_out : nat` field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did`.
2. Require the SNS governance proposal to specify this minimum, computed off-chain at proposal creation time using the current pool price with an acceptable slippage tolerance (e.g., 1–2%).
3. In `execute_treasury_manager_deposit`, pass the minimum through to the DEX deposit call and treat a shortfall as a hard error that reverts the allowance.
4. Alternatively, enforce a maximum time-to-execute window: if the proposal is not executed within N blocks of the voting period ending, the deposit is cancelled and the allowance is revoked, preventing stale-price execution.

### Proof of Concept
1. SNS governance proposal is submitted: deposit 100,000 SNS tokens + 50,000 ICP into a DEX pool. The current pool ratio implies the treasury should receive ~10,000 LP tokens.
2. During the 4-day voting period, attacker buys SNS tokens from the pool, shifting the SNS/ICP ratio so that 100,000 SNS + 50,000 ICP now maps to only ~7,000 LP tokens.
3. Proposal passes and `execute_treasury_manager_deposit` is called. `approve_treasury_manager` grants the allowance; `deposit` is called with `DepositRequest { allowances: [100_000 SNS, 50_000 ICP] }` — no minimum output.
4. The DEX accepts the deposit at the manipulated ratio and mints 7,000 LP tokens to the treasury manager.
5. Attacker sells their SNS tokens back, restoring the pool price and pocketing the spread (~3,000 LP tokens worth of value extracted from the SNS treasury).
6. The `execute_treasury_manager_deposit` call returns `Ok` with no indication that the treasury received fewer LP tokens than expected. [6](#0-5) [2](#0-1)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
