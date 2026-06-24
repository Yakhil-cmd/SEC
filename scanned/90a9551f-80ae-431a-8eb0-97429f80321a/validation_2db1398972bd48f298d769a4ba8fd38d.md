### Title
Missing Slippage Protection in SNS Treasury Manager DEX Deposit Allows Sandwich Attacks on SNS Treasury Assets - (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager deposit API and its governance-layer execution provide no mechanism to enforce a minimum LP token output when depositing SNS treasury assets into DEX liquidity pools. The `DepositRequest` type contains only token allowances with no `min_lp_tokens_out` field, and the governance execution path performs no price-ratio or minimum-output validation. This is structurally identical to the reported `amountOutMinimum = 0` pattern: treasury funds are committed to a DEX deposit with zero slippage protection, enabling sandwich attacks that permanently drain SNS treasury value.

---

### Finding Description

The `TreasuryManager` API, defined in `rs/sns/treasury_manager/treasury_manager.did`, exposes a `deposit` endpoint that accepts a `DepositRequest` containing only `allowances` (token amounts):

```
type DepositRequest = record {
  allowances : vec Allowance;
};
```

There is no field for a minimum LP token output, a maximum acceptable price deviation, or any other slippage guard. The file itself acknowledges this under "Known Security Risks":

> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved." [1](#0-0) 

The governance-layer execution in `rs/sns/governance/src/extensions.rs`, function `execute_treasury_manager_deposit`, approves ICRC-2 allowances for both SNS tokens and ICP, then calls `deposit` on the extension canister with no minimum output check:

```rust
governance.approve_treasury_manager(
    extension_canister_id,
    treasury_allocation_sns_e8s,
    treasury_allocation_icp_e8s,
).await?;

let balances = governance.env.call_canister(
    extension_canister_id, "deposit", arg_blob
).await ...
``` [2](#0-1) 

The validation function `validate_deposit_operation_impl` only checks that the requested amounts do not exceed 50% of the current treasury balance. It performs no price-ratio check, no minimum LP token output check, and no staleness check on the DEX price relative to the time the proposal was created: [3](#0-2) 

The proposal rendering in `rs/sns/governance/src/proposal.rs` includes a WARNING about this risk but provides no enforcement:

> "This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account." [4](#0-3) 

The caveat "undeposited tokens are automatically returned" only covers the case where the DEX rejects the deposit entirely. It does not cover the case where the deposit succeeds at a manipulated, unfavorable price ratio — in that case, the value loss is permanent and unrecoverable.

---

### Impact Explanation

An attacker who observes an adopted SNS governance deposit proposal can sandwich-attack the DEX deposit:

1. Attacker submits a large swap on the DEX to move the pool price unfavorably before the governance canister executes the deposit.
2. The governance canister executes `execute_treasury_manager_deposit`, which calls `deposit` on the KongSwap adaptor (or any blessed TreasuryManager implementation) with the full approved allowance and no minimum output constraint.
3. The adaptor deposits at the manipulated price, receiving far fewer LP tokens than the SNS treasury would receive at fair market price.
4. Attacker back-runs to restore the price and extract the arbitrage profit.

The SNS treasury permanently loses the difference in value between the fair-price LP tokens and the manipulated-price LP tokens. Since SNS treasury deposits can be up to 50% of the treasury balance per proposal, the loss can be substantial.

**Impact: Medium** — permanent loss of SNS treasury assets (ICP and SNS tokens) proportional to the price manipulation achievable on the target DEX.

---

### Likelihood Explanation

**Likelihood: Medium.**

- SNS governance proposals are public and their adoption is observable on-chain by any principal.
- The IC execution model allows an attacker to submit DEX manipulation transactions after observing proposal adoption, before or during the governance canister's asynchronous execution of the deposit.
- The KongSwap adaptor (`kongswap-adaptor-canister`) is already deployed and blessed, making this a live attack surface.
- The attack requires no privileged access — any unprivileged canister caller or boundary node user can submit DEX transactions.
- The only constraint is having sufficient capital to move the DEX price, which is feasible for a well-funded attacker targeting a high-value SNS treasury deposit.

---

### Recommendation

1. **Extend the `DepositRequest` type** in `rs/sns/treasury_manager/treasury_manager.did` to include a `min_lp_tokens_out` or `max_price_deviation_bps` field, allowing the SNS governance proposal to specify an acceptable slippage bound at proposal creation time.

2. **Enforce the minimum output** in `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` by requiring the `TreasuryManager` implementation to return the actual LP tokens received and comparing against the governance-specified minimum.

3. **Add price-staleness validation** in `validate_deposit_operation_impl`: query the DEX for the current price at proposal execution time and reject if it deviates beyond a governance-configured threshold from the price at proposal creation time.

4. **Update the `TreasuryManager` trait** in `rs/sns/treasury_manager/src/lib.rs` to require implementations to enforce the minimum output constraint passed in the `DepositRequest`.

---

### Proof of Concept

**Setup**: An SNS has 10,000 ICP and 1,000,000 SNS tokens in its treasury. A governance proposal is adopted to deposit 5,000 ICP and 500,000 SNS tokens into a KongSwap liquidity pool.

**Attack**:

1. Attacker observes the proposal adoption via `get_proposal` on the SNS governance canister (public query).
2. Attacker submits a large ICP→SNS swap on KongSwap, moving the ICP/SNS price 20% against the SNS treasury's deposit direction.
3. The SNS governance canister executes `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` grants the KongSwap adaptor ICRC-2 allowances for 5,000 ICP and 500,000 SNS tokens.
   - `call_canister(..., "deposit", ...)` is called with no minimum LP output.
4. The KongSwap adaptor deposits at the manipulated 20%-worse price, receiving LP tokens worth ~4,000 ICP equivalent instead of ~5,000 ICP equivalent.
5. Attacker back-runs with a SNS→ICP swap, restoring the price and pocketing ~1,000 ICP equivalent in profit.
6. The SNS treasury has permanently lost ~1,000 ICP equivalent in value with no recourse.

**Root cause lines**:
- `rs/sns/treasury_manager/treasury_manager.did` lines 84–86: `DepositRequest` has no minimum output field. [5](#0-4) 
- `rs/sns/governance/src/extensions.rs` lines 1566–1578: deposit executed with no minimum output enforcement. [6](#0-5) 
- `rs/sns/governance/src/extensions.rs` lines 276–320: validation checks only balance limits, not price ratios. [3](#0-2)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
