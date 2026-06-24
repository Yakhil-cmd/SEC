### Title
Missing Slippage Protection in SNS Treasury Manager Deposit/Withdraw API Allows Price Manipulation - (File: rs/sns/treasury_manager/src/lib.rs, rs/sns/treasury_manager/treasury_manager.did, rs/sns/governance/src/extensions.rs)

### Summary
The SNS Treasury Manager framework, which allows SNS DAOs to deposit treasury funds (ICP and SNS tokens) into on-chain DEX liquidity pools, defines a `DepositRequest` type and `TreasuryManager` trait that contain no `min_amount_out` or slippage protection parameter. The governance execution path calls `deposit` on the treasury manager canister without enforcing any minimum received LP tokens. This is the direct IC analog of the Hypervisor sandwiching vulnerability: the `DepositRequest` is the IC equivalent of `pool.mint`/`pool.burn` called without `amountMin`.

### Finding Description
The `DepositRequest` type in `rs/sns/treasury_manager/src/lib.rs` and `treasury_manager.did` contains only `allowances` (the maximum tokens the treasury manager may consume), with no field for a minimum acceptable LP token return or minimum price ratio:

```rust
// rs/sns/treasury_manager/src/lib.rs (via treasury_manager.did)
type DepositRequest = record {
  allowances : vec Allowance;  // No min_lp_tokens, no min_amount_out, no slippage_bps
};
```

The `TreasuryManager` trait's `deposit` function signature enforces no slippage constraint:

```rust
fn deposit(
    &mut self,
    request: DepositRequest,
) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;
```

The governance execution path in `execute_treasury_manager_deposit` approves the allowance and calls `deposit` without any post-call validation of how many LP tokens were received:

```rust
// 1. Transfer funds from treasury to treasury manager
governance.approve_treasury_manager(...).await?;
// 2. Call deposit on treasury manager — no min_received check
let balances = governance.env.call_canister(extension_canister_id, "deposit", arg_blob).await...?;
```

The validation step `validate_deposit_operation_impl` only checks that the requested amount does not exceed 50% of the current treasury balance at proposal submission time. It does not enforce any minimum return from the DEX at execution time.

The codebase itself acknowledges this in `treasury_manager.did`:

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

And in the proposal rendering warning in `rs/sns/governance/src/proposal.rs`:

```
Some Decentralized Exchanges lack slippage protection during deposits. Consequently,
deposited asset ratios may deviate from those specified in the proposal.
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running
or sandwich attacks.
```

The root cause is the IC-internal design choice: the `DepositRequest` type and `TreasuryManager` trait do not include a `min_lp_tokens` or equivalent slippage parameter, and the governance framework does not validate the outcome of the deposit call.

### Impact Explanation
An attacker who can interact with the same on-chain DEX canister as the treasury manager can manipulate the pool price before the treasury deposit executes and restore it after, extracting value from the SNS treasury. Because SNS governance proposals have a voting period (days), the attacker has a predictable execution window. The treasury manager is approved to spend up to 50% of the SNS treasury's ICP and SNS token balances per deposit proposal. A successful sandwich attack could cause the treasury to receive far fewer LP tokens than the deposited value warrants, permanently destroying SNS DAO treasury value. The "any undeposited tokens are automatically returned" mitigation only covers excess tokens not consumed by the DEX, not the loss from receiving LP tokens at a manipulated price.

### Likelihood Explanation
The SNS Treasury Manager framework is designed to interact with on-chain DEX canisters (e.g., KongSwap, as evidenced by `KongSwapAdaptor` references in integration tests). Any canister that can call the same DEX can manipulate pool prices. Since governance proposals are public and have predictable execution timing, an attacker can observe a pending deposit proposal, calculate the optimal sandwich, and execute it. The governance voting period (typically days) gives ample time to prepare. The likelihood is medium-high for any SNS that activates a treasury manager extension pointing to a DEX without native slippage protection.

### Recommendation
1. Add a `min_lp_tokens_out` (or equivalent) field to `DepositRequest` in `rs/sns/treasury_manager/src/lib.rs` and `treasury_manager.did`, analogous to the `amountMin` recommendation in the Hypervisor report.
2. Require `TreasuryManager` implementations to enforce this minimum in their `deposit` logic.
3. In `validate_deposit_operation_impl` (`rs/sns/governance/src/extensions.rs`), require that the `DepositRequest` includes a non-zero `min_lp_tokens_out` field.
4. In `execute_treasury_manager_deposit`, validate the returned `Balances` to confirm that the external custodian balance increased by at least the expected minimum.

### Proof of Concept

1. An SNS DAO submits a governance proposal to deposit 1,000,000 ICP and 10,000,000 SNS tokens into a KongSwap liquidity pool via `ExecuteExtensionOperation { operation_name: "deposit", ... }`.
2. The proposal enters the voting period (publicly visible on-chain).
3. An attacker observes the pending proposal and the target DEX pool.
4. When the proposal is adopted and `execute_treasury_manager_deposit` is called, the attacker front-runs by calling the DEX to buy SNS tokens, moving the price ratio unfavorably for the treasury.
5. The governance canister calls `approve_treasury_manager` (granting ICRC-2 allowance) then calls `deposit` on the treasury manager canister.
6. The treasury manager calls the DEX `deposit`/`add_liquidity` with the approved tokens at the manipulated price, receiving far fewer LP tokens than fair value.
7. The attacker immediately sells their SNS tokens back, restoring the price and pocketing the spread.
8. The `execute_treasury_manager_deposit` function receives a successful `TreasuryManagerResult` with no minimum LP token check, so the governance call succeeds despite the treasury loss.
9. The SNS DAO has permanently lost treasury value with no on-chain recourse, since the `DepositRequest` contained no `min_lp_tokens_out` constraint.

**Key files:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/sns/treasury_manager/src/lib.rs (L250-261)
```rust
pub trait TreasuryManager {
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

    /// Implements the `withdraw` API function.
    fn withdraw(
        &mut self,
        request: WithdrawRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;
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
