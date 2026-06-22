### Title
No Slippage Protection in SNS Treasury Manager Deposit Exposes DAO Funds to Price Manipulation - (File: rs/sns/governance/src/extensions.rs)

### Summary
The `execute_treasury_manager_deposit` function in the SNS governance canister calls `deposit` on a treasury manager extension (e.g., a DEX liquidity pool adaptor) without enforcing any minimum LP token output or deadline. The `DepositRequest` API type itself contains no `min_lp_amount_out` or `deadline` field, making it structurally impossible for the SNS governance to specify acceptable slippage bounds. An unprivileged canister or user who can interact with the target DEX can manipulate the pool ratio between SNS proposal approval and execution, causing the SNS treasury to receive fewer LP tokens than expected.

### Finding Description
The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` performs two steps: it approves the treasury manager to spend SNS and ICP tokens via ICRC-2, then calls `deposit` on the extension canister. The result is only checked for `Ok`/`Err` — no minimum LP token amount is validated. [1](#0-0) 

The `DepositRequest` type defined in the treasury manager API contains only `allowances` (how much to deposit) and no `min_lp_amount_out` or `deadline` field: [2](#0-1) 

The `ValidatedDepositOperationArg` struct, which is the validated form of the deposit proposal argument, similarly carries only `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` — no minimum output constraint: [3](#0-2) 

The validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance. It does not check any expected LP token output: [4](#0-3) 

The ICRC-2 approval granted to the treasury manager has a 1-hour expiry, but this is only the approval window — it is not a slippage deadline enforced at the DEX level: [5](#0-4) 

The codebase itself acknowledges this risk in the treasury manager DID file: [6](#0-5) 

And in the proposal rendering for `RegisterExtension`: [7](#0-6) 

### Impact Explanation
An SNS DAO that votes to deposit treasury funds into a DEX liquidity pool via a treasury manager extension will receive an indeterminate number of LP tokens. Because neither the `DepositRequest` API nor the `execute_treasury_manager_deposit` execution path enforces a `min_lp_amount_out`, the SNS treasury can receive significantly fewer LP tokens than the DAO voters expected at proposal creation time. This constitutes a direct, quantifiable loss of DAO-owned funds — the difference between the expected and actual LP token value is permanently lost to the treasury.

### Likelihood Explanation
SNS proposals have voting periods that can last days. Any unprivileged user or canister with access to the target DEX can manipulate the pool ratio during this window. On the Internet Computer, there is no traditional mempool front-running, but the large time gap between proposal approval and execution (governance voting period + execution delay) gives an attacker ample opportunity to skew the pool price before the deposit executes, then reverse the manipulation afterward to extract profit. The attack requires no privileged access — only the ability to interact with the same DEX the treasury manager uses.

### Recommendation
1. Add a `min_lp_amount_out` field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did` so that the SNS governance can specify the minimum acceptable LP token output.
2. Add a `deadline` field to `DepositRequest` so that the deposit reverts if executed after a specified timestamp.
3. In `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`), validate the returned `Balances` against the minimum expected LP token amount specified in the proposal, and return an error if the output is below the threshold.
4. Require that `ValidatedDepositOperationArg` carries `min_lp_amount_out` and `deadline` fields, validated at proposal submission time.

### Proof of Concept
1. An SNS DAO submits a proposal to deposit 1000 SNS tokens and 1000 ICP into a DEX pool at an expected 1:1 ratio, expecting ~2000 LP tokens.
2. The proposal enters the voting period (e.g., 4 days).
3. An attacker deposits a large amount of ICP into the same DEX pool, skewing the ratio to 1 SNS : 10 ICP.
4. The SNS governance proposal passes and `execute_treasury_manager_deposit` is called. The treasury manager calls the DEX `deposit` with the approved amounts. Due to the skewed ratio, the SNS treasury receives far fewer LP tokens than expected (e.g., ~100 LP tokens instead of ~2000).
5. The attacker withdraws their ICP from the pool, restoring the ratio and pocketing the arbitrage profit.
6. `execute_treasury_manager_deposit` returns `Ok` because the deposit call succeeded — no minimum output check is performed. [8](#0-7)

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

**File:** rs/sns/governance/src/extensions.rs (L1566-1609)
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

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
```

**File:** rs/sns/governance/src/extensions.rs (L1663-1672)
```rust
/// Validated deposit operation arguments
#[derive(Debug, Clone)]
pub struct ValidatedDepositOperationArg {
    /// Amount of SNS tokens to allocate from treasury
    pub treasury_allocation_sns_e8s: u64,
    /// Amount of ICP tokens to allocate from treasury
    pub treasury_allocation_icp_e8s: u64,
    /// Original Precise value with all fields
    pub original: Precise,
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
