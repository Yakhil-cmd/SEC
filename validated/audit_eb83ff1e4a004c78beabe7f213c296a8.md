### Title
No Slippage Protection in SNS Treasury Manager Deposit Interface Enables Price Manipulation of DAO Treasury Deposits - (File: `rs/sns/treasury_manager/src/lib.rs`, `rs/sns/governance/src/extensions.rs`)

### Summary

The SNS Treasury Manager deposit interface (`DepositRequest`) contains no slippage protection parameters. When an SNS DAO passes a governance proposal to deposit treasury assets into a DEX liquidity pool, the exact deposit amounts are publicly visible for the entire voting period (days to weeks). Any actor can manipulate the DEX pool price during this window, causing the DAO to receive significantly fewer LP tokens than expected. The codebase itself acknowledges this risk in comments but provides no enforcement mechanism.

### Finding Description

The `DepositRequest` struct in the Treasury Manager library contains only `allowances` (the amounts to deposit) with no field for minimum LP tokens to receive or maximum price deviation:

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` constructs the deposit call via `construct_treasury_manager_deposit_payload`, which only encodes the token allowances — no slippage bound is ever passed to the treasury manager canister:

```rust
let arg = DepositRequest { allowances };
```

The governance proposal for a `TreasuryManagerDeposit` operation only accepts `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` as parameters. There is no `minimum_lp_tokens_out`, `max_price_deviation_bps`, or equivalent field anywhere in the proposal payload, the `DepositRequest` type, or the `TreasuryManager` trait.

The codebase itself acknowledges this in two places. In `rs/sns/treasury_manager/treasury_manager.did`:

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

And in the proposal rendering for `RegisterExtension` in `rs/sns/governance/src/proposal.rs`:

```
## WARNING
Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

The disclaimer that "undeposited tokens are automatically returned" does not protect against the core loss: the DAO receives fewer LP tokens than it should for the tokens it *does* deposit.

### Impact Explanation

An SNS DAO treasury deposit into a DEX liquidity pool can be sandwiched. The attacker receives the price difference at the DAO's expense. Since SNS treasury funds are DAO-owned assets, this constitutes a direct loss of DAO funds. The loss scales with the deposit size — large treasury deposits (which are the primary use case) are the most profitable targets.

### Likelihood Explanation

Unlike Ethereum MEV, the IC governance model makes this *more* exploitable, not less. The governance voting period is public and lasts days to weeks. The exact deposit amounts (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`) are encoded in the proposal and visible to all observers from the moment the proposal is submitted. An attacker has a large, predictable window to manipulate the DEX pool ratio before the proposal executes. No special access, no private keys, and no subnet-majority corruption is required — only the ability to trade on the same DEX.

### Recommendation

1. Add a `minimum_lp_tokens_out: Option<Nat>` field to `DepositRequest` in `rs/sns/treasury_manager/src/lib.rs`.
2. Add a corresponding `minimum_lp_tokens_out` field to the governance proposal payload parsed in `rs/sns/governance/src/extensions.rs` (`construct_deposit_allowances` / `construct_treasury_manager_deposit_payload`).
3. Require TreasuryManager implementations to enforce this bound and revert if the DEX returns fewer LP tokens than the specified minimum.
4. Validate that the field is present and non-zero during proposal validation in `validate_execute_extension_operation`.

### Proof of Concept

1. SNS DAO submits a `TreasuryManagerDeposit` proposal specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y` to deposit into a DEX pool.
2. The proposal enters the voting period (publicly visible, days long).
3. Attacker observes the proposal and swaps a large amount of ICP for SNS tokens on the target DEX, skewing the pool ratio so SNS tokens are overpriced relative to ICP.
4. The proposal passes and `execute_treasury_manager_deposit` is called. It calls `approve_treasury_manager` then `deposit` on the treasury manager canister with `DepositRequest { allowances }` — no slippage bound.
5. The treasury manager deposits at the manipulated ratio. The DAO receives far fewer LP tokens than it would at the fair price.
6. Attacker swaps back (sells SNS for ICP), restoring the pool ratio and pocketing the profit extracted from the DAO's deposit.

The root cause is confirmed at: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

**File:** rs/sns/governance/src/extensions.rs (L1088-1099)
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;

    let arg = DepositRequest { allowances };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding DepositRequest: {err}"))?;

    Ok(arg)
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1550)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

## Extension Configuration

The extension will be deployed and configured according to the provided parameters.",
    ))
```
