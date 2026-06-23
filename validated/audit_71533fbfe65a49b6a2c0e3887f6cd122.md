### Title
SNS Treasury Manager Withdrawal Lacks Slippage Protection, Enabling Pool-Imbalance Grief of DAO Treasury - (File: rs/sns/treasury_manager/treasury_manager.did, rs/sns/governance/src/extensions.rs)

### Summary

The SNS Treasury Manager framework (`treasury_manager.did` + `extensions.rs`) provides no mechanism for the SNS DAO to specify a minimum acceptable amount when withdrawing liquidity from an external DEX (e.g., KongSwap). The `WithdrawRequest` type carries no `min_amount_out` field, and `ValidatedWithdrawOperationArg` explicitly rejects all arguments. Any unprivileged user who swaps tokens in the pool between proposal approval and execution can shift the pool ratio, causing the SNS treasury to receive materially fewer tokens than it deposited, with the difference captured by other LPs.

### Finding Description

The `WithdrawRequest` Candid type in `treasury_manager.did` contains only an optional `withdraw_accounts` field and no slippage bound:

```candid
type WithdrawRequest = record {
  withdraw_accounts : opt vec record { principal; Account };
};
``` [1](#0-0) 

The Rust-side validation in `extensions.rs` enforces that the withdraw operation accepts **no arguments at all**:

```rust
// For now, only allow empty arguments
if original.value.is_some() {
    return Err("Withdraw operation does not accept arguments at this time".to_string());
}
``` [2](#0-1) 

The `execute_treasury_manager_withdraw` function calls the extension canister's `withdraw` endpoint unconditionally, with no post-call check that the returned balances meet any minimum: [3](#0-2) 

The `treasury_manager.did` itself acknowledges the deposit-side slippage risk but does not acknowledge or mitigate the withdrawal-side risk:

```
// Known Security Risks:
// Some liquidity pools do not implement slippage protection
// for deposits.
``` [4](#0-3) 

The proposal-rendering warning in `proposal.rs` also only covers the deposit direction, noting that "any undeposited tokens are automatically returned to the SNS treasury account": [5](#0-4) 

No analogous return-of-excess mechanism exists for withdrawals. The integration test confirms that a deposit with a mismatched ratio returns excess ICP to the treasury owner, but no equivalent test or guard exists for the withdrawal path: [6](#0-5) 

### Impact Explanation

An SNS DAO that has deposited SNS tokens and ICP into a KongSwap liquidity pool and later passes a `withdraw` proposal will receive whatever the pool's current ratio dictates at execution time. If the pool has been shifted by external swaps (even by normal market activity), the treasury receives fewer tokens than it deposited. The shortfall accrues to other LPs in the pool. Because the SNS governance voting period can span days, the window for pool manipulation is large. There is no on-chain mechanism for the DAO to abort or bound the loss.

This is a **ledger conservation bug**: the SNS treasury's token balance after a deposit-then-withdraw round-trip can be less than before, with no protocol-level protection against the deficit.

### Likelihood Explanation

The SNS governance voting period creates a multi-day window between proposal approval and execution. Any user who can call the KongSwap DEX canister (an unprivileged ingress call) can shift the pool ratio during this window. No special role, key, or majority is required. Normal market trading activity alone can cause the loss without deliberate attack. The `ValidatedWithdrawOperationArg` code path is reachable by any SNS token holder who can submit and pass a governance proposal.

### Recommendation

1. Add a `min_amount_out` field (per asset) to `WithdrawRequest` in `treasury_manager.did`.
2. Remove the blanket rejection of withdraw arguments in `ValidatedWithdrawOperationArg::try_from` and validate the minimum-amount fields instead.
3. In `execute_treasury_manager_withdraw`, compare the returned `Balances` against the specified minimums and surface a `GovernanceError` if they are not met, so the proposal fails rather than silently accepting a loss.
4. Add the same slippage-risk warning to the `withdraw` proposal rendering that currently exists only for `register_extension` (deposit direction).

### Proof of Concept

1. SNS DAO passes a `RegisterExtension` proposal, depositing `D_sns` SNS tokens and `D_icp` ICP into KongSwap at ratio `R_deposit`.
2. Between proposal approval and execution of a subsequent `withdraw` proposal, an attacker calls the KongSwap DEX canister (unprivileged ingress) to swap a large amount of ICP for SNS tokens, shifting the pool ratio to `R_shifted` (more ICP per SNS than `R_deposit`).
3. The `withdraw` proposal executes via `execute_treasury_manager_withdraw` → `call_canister(extension_canister_id, "withdraw", arg_blob)`. No minimum-amount check is performed.
4. KongSwap returns tokens at ratio `R_shifted`. The treasury receives `D_sns' < D_sns` SNS tokens (or `D_icp' < D_icp` ICP, depending on shift direction). The shortfall remains in the pool.
5. The `GovernanceError` path is never triggered; the proposal is marked as successfully executed despite the treasury loss.

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L88-93)
```text
type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
};
```

**File:** rs/sns/governance/src/extensions.rs (L1612-1661)
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
}
```

**File:** rs/sns/governance/src/extensions.rs (L1739-1743)
```rust
        // For now, only allow empty arguments
        // This ensures withdraw operations don't accept parameters yet
        if original.value.is_some() {
            return Err("Withdraw operation does not accept arguments at this time".to_string());
        }
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

**File:** rs/nervous_system/integration_tests/tests/sns_extension_test.rs (L454-457)
```rust
        // Second deposit takes place with deposit ratio (SNS/ICP)
        // lower than the market ratio (SNS/ICP in the pool). Hence,
        // the excess amount of ICP is returned to the treasury owner.
        let expected_icp_fee_collector = 9 * ICP_FEE;
```
