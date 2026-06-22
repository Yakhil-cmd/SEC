### Title
SNS Treasury Funds Deposited into DEX Liquidity Pools Without Slippage Protection, Enabling Front-Running and Price Manipulation - (File: rs/sns/governance/src/extensions.rs)

### Summary

The SNS `TreasuryManagerDeposit` operation, executed via governance proposal, deposits SNS treasury funds (ICP and SNS tokens) into external DEX liquidity pools without any slippage parameter or minimum-output validation. The `DepositRequest` type in the `treasury_manager.did` interface has no `min_lp_tokens_out` or equivalent field, and `execute_treasury_manager_deposit` does not validate the actual balances returned after the deposit. This is structurally analogous to the `reimburseLiquidityFees()` / `swapExactIn()` vulnerability in the original report: SNS treasury assets can be deposited at arbitrarily unfavorable ratios, with no on-chain enforcement of a minimum acceptable output.

### Finding Description

The `DepositRequest` type defined in `rs/sns/treasury_manager/treasury_manager.did` contains only `allowances` (the amounts to deposit) and no slippage or minimum-output field:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The file itself acknowledges this as a "Known Security Risk":

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved. [2](#0-1) 

The governance proposal renderer also warns about this in `validate_and_render_register_extension`:

> Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks. [3](#0-2) 

However, the warning is only informational text in the proposal rendering — there is no enforcement. The execution path in `execute_treasury_manager_deposit` calls the DEX canister's `deposit` method and only checks that the call returns `Ok(Balances)`, without validating the actual LP tokens received or the effective exchange ratio: [4](#0-3) 

The validation step `validate_deposit_operation_impl` only checks that the requested amount does not exceed 50% of the current treasury balance — it performs no price or slippage check: [5](#0-4) 

Because `DepositRequest` has no slippage field, even a DEX that supports slippage protection cannot be used with it — the protocol structurally prevents slippage bounds from being specified.

### Impact Explanation

An attacker who can manipulate the DEX pool state (e.g., a large liquidity provider, or the DEX canister operator if it is not fully decentralized) can cause the SNS treasury to deposit tokens at a severely unfavorable ratio. The governance code accepts any `Ok` response from the DEX canister's `deposit` method without checking the effective output. SNS treasury funds (ICP and SNS tokens) can be permanently lost to the liquidity pool at an attacker-controlled exchange rate. The impact is direct, irreversible loss of DAO treasury assets.

### Likelihood Explanation

The attack window is the entire governance voting period (which can be days). During this time, an attacker can observe the pending `TreasuryManagerDeposit` proposal and manipulate the DEX pool price before the proposal executes. On the Internet Computer, there is no mempool ordering, but the attacker can submit their pool-manipulation transaction in the same round or immediately before the governance execution. Additionally, a malicious or compromised DEX canister could simply return 0 LP tokens and the governance code would accept it as a successful deposit. The risk is elevated because the `DepositRequest` type makes it structurally impossible to specify a minimum output, even if the DEX supports it.

### Recommendation

1. Add a `min_lp_tokens_out` (or equivalent minimum-output) field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did`.
2. In `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`), validate the `Balances` returned by the DEX against the expected minimum output specified in the proposal.
3. In `validate_deposit_operation_impl`, require that the proposal includes a slippage tolerance or minimum LP token output, and reject proposals that omit it.
4. Consider adding a post-deposit check: if the effective ratio deviates beyond a governance-configured threshold, revert the deposit (or at minimum emit a critical log and halt further deposits).

### Proof of Concept

1. SNS governance adopts a `TreasuryManagerDeposit` proposal specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y`.
2. During the voting period, an attacker (or the DEX canister operator) manipulates the KongSwap pool to skew the SNS/ICP ratio severely.
3. The proposal executes: `approve_treasury_manager` grants ICRC-2 allowances for `X` SNS tokens and `Y` ICP to the treasury manager canister. [6](#0-5) 
4. `call_canister(extension_canister_id, "deposit", arg_blob)` is called. The DEX deposits at the manipulated ratio, returning far fewer LP tokens than expected. [7](#0-6) 
5. The governance code checks only that the result is `Ok(Balances)` and logs it — no minimum output is enforced. The SNS treasury has permanently lost value. [8](#0-7) 
6. The `DepositRequest` type has no field to have prevented this: [1](#0-0)

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

**File:** rs/sns/governance/src/extensions.rs (L276-320)
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
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1573)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;
```

**File:** rs/sns/governance/src/extensions.rs (L1575-1607)
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
```
