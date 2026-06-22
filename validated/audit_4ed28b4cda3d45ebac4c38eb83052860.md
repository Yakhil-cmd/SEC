### Title
No Slippage Protection in SNS Treasury Manager Deposit API — (`rs/sns/treasury_manager/src/lib.rs`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The `DepositRequest` type used by the SNS Treasury Manager API contains no minimum acceptable output amount fields. When SNS governance executes a treasury deposit into a DEX liquidity pool, it cannot enforce any price bounds or minimum LP token receipts. This is structurally identical to passing `amountAMin = 0` and `amountBMin = 0` to Uniswap's `addLiquidity`. The codebase itself acknowledges this as a "Known Security Risk."

---

### Finding Description

The `DepositRequest` struct in `rs/sns/treasury_manager/src/lib.rs` is defined as:

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

It carries only the token allowances — no `min_lp_tokens_out`, no `min_token_a_amount`, no `min_token_b_amount`, and no deadline. [1](#0-0) 

The governance-side validation function `validate_deposit_operation_impl` checks only that the requested amounts do not exceed 50% of the current treasury balance. It performs no validation of minimum acceptable output amounts or price bounds. [2](#0-1) 

The execution function `execute_treasury_manager_deposit` approves the treasury manager to spend the tokens and then calls `deposit` — forwarding only the `DepositRequest` with allowances, with no minimum output constraints attached. [3](#0-2) 

The DID interface file explicitly acknowledges this structural gap under "Known Security Risks":

> *Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved.* [4](#0-3) 

The proposal rendering code in `rs/sns/governance/src/proposal.rs` also warns voters about this, but the warning is purely informational — no enforcement exists at the protocol level. [5](#0-4) 

---

### Impact Explanation

An SNS treasury deposit proposal is public on-chain before execution. A malicious actor who observes the pending proposal can front-run it by manipulating the DEX pool price (sandwich attack):

1. Attacker front-runs: swaps a large amount into the pool to skew the price ratio.
2. Governance deposit executes: SNS treasury tokens are deposited at the manipulated ratio, receiving far fewer LP tokens than expected.
3. Attacker back-runs: reverses their swap, extracting value from the pool at the treasury's expense.

Because `DepositRequest` carries no minimum output fields, the treasury manager implementation has no governance-approved price bound to enforce. Even a well-implemented treasury manager cannot receive slippage parameters from governance because the API structurally omits them. The result is a direct, quantifiable loss of SNS treasury value — the treasury receives fewer LP tokens than the deposited assets are worth at the pre-manipulation price.

---

### Likelihood Explanation

SNS governance proposals are fully public and have a multi-day voting period before execution. The execution time is predictable. Any actor monitoring the IC governance canister can observe a pending `ExecuteExtensionOperation` deposit proposal, compute the expected pool impact, and profitably sandwich it. No privileged access is required — only the ability to hold tokens in the relevant DEX pool. Likelihood is **high** for any SNS that deploys a treasury manager against a pool with sufficient liquidity to make the attack profitable.

---

### Recommendation

Add minimum acceptable output amount fields to `DepositRequest` in `rs/sns/treasury_manager/src/lib.rs`:

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
    /// Minimum LP tokens (or equivalent) the treasury must receive.
    /// Deposit must fail if the DEX returns fewer.
    pub min_lp_tokens_out: Option<Nat>,
    /// Per-asset minimum amounts acceptable for deposit.
    pub min_amounts_out: Option<BTreeMap<Principal, Nat>>,
}
```

The governance validation in `validate_deposit_operation_impl` (`rs/sns/governance/src/extensions.rs`) should require these fields to be present and non-zero when the target extension type is a DEX liquidity pool adaptor. The treasury manager implementation must then enforce these bounds when calling the underlying DEX, rejecting the deposit if the DEX would return less than the governance-approved minimum. [1](#0-0) [6](#0-5) 

---

### Proof of Concept

1. An SNS deploys a treasury manager extension pointing to a DEX liquidity pool.
2. SNS governance passes a proposal: `ExecuteExtensionOperation { operation_name: "deposit", operation_arg: { treasury_allocation_sns_e8s: X, treasury_allocation_icp_e8s: Y } }`.
3. The proposal is public. An attacker observes it during the voting period.
4. Immediately before the proposal executes, the attacker swaps a large amount of ICP into the pool, driving up the SNS/ICP price ratio.
5. `execute_treasury_manager_deposit` fires: governance approves the treasury manager for `X` SNS tokens and `Y` ICP, then calls `deposit` with a `DepositRequest { allowances: [...] }` — no minimum output field exists.
6. The treasury manager deposits at the manipulated ratio. The SNS treasury receives LP tokens worth significantly less than `X` SNS + `Y` ICP at the pre-attack price.
7. The attacker back-runs, restoring the pool price and pocketing the difference.

The root cause — the missing minimum output fields in `DepositRequest` — is confirmed at: [1](#0-0) [7](#0-6)

### Citations

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
