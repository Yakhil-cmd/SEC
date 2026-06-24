### Title
Missing Slippage Protection in SNS Treasury Manager Deposit Operations - (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS Treasury Manager deposit flow, executed via `ExecuteExtensionOperation` governance proposals, lacks any on-chain slippage protection. The `DepositRequest` type has no `min_amount_out` or minimum LP-token field, and `execute_treasury_manager_deposit` never validates the output received from the DEX. This is a direct IC analog to the `amountOutMin = 1` pattern in the external report.

---

### Finding Description

The `DepositRequest` type defined in `rs/sns/treasury_manager/treasury_manager.did` contains only `allowances` (input token amounts) with no field for a minimum acceptable output:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

The file itself acknowledges the risk in a comment block:

> *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."*

The governance execution path in `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`, lines 1545–1609) constructs the payload via `construct_treasury_manager_deposit_payload` (lines 1088–1098), which builds a `DepositRequest` with only `allowances`, then calls `deposit` on the Treasury Manager canister:

```rust
let balances = governance
    .env
    .call_canister(extension_canister_id, "deposit", arg_blob)
    .await
    ...?;
```

The result is decoded and logged, but **no minimum output is checked**. The governance code has no mechanism to enforce that the SNS treasury received at least a specified number of LP tokens in return for the deposited ICP and SNS tokens.

The proposal rendering function `validate_and_render_register_extension` (`rs/sns/governance/src/proposal.rs`, lines 1540–1545) adds only a human-readable WARNING string — it provides no on-chain enforcement:

```
## WARNING
Some Decentralized Exchanges lack slippage protection during deposits. Consequently,
deposited asset ratios may deviate from those specified in the proposal.
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

The warning is informational only. There is no `min_lp_tokens_out` parameter that voters can set, and no post-execution check that the output meets a threshold.

---

### Impact Explanation

When an `ExecuteExtensionOperation` deposit proposal is adopted and executed, the SNS governance canister transfers ICP and SNS tokens to the Treasury Manager, which then deposits them into a DEX liquidity pool (e.g., KongSwap on IC). Because no minimum output is enforced:

- The SNS treasury can receive far fewer LP tokens than the deposited assets are worth at the time of proposal adoption.
- The difference in value is permanently lost to the SNS treasury (i.e., to all SNS token holders).
- The `DepositRequest` API design makes it structurally impossible for any future Treasury Manager implementation to receive a caller-specified minimum output from governance, because the field does not exist in the interface.

---

### Likelihood Explanation

SNS governance proposals have a voting period that can span multiple days. Between proposal adoption and execution (which occurs in the next heartbeat after the voting period ends), the DEX pool price can shift substantially due to:

1. **Organic market movement** — normal trading activity on the IC-native DEX changes the pool ratio.
2. **Targeted price manipulation** — any unprivileged DEX user can trade to move the pool price unfavorably immediately before the governance heartbeat executes the deposit, then trade back afterward to extract value. The execution time is predictable (end of voting period), making this a realistic sandwich-style attack on IC.

No privileged access is required for the price manipulation step. The attacker only needs to be a DEX user.

---

### Recommendation

1. **Add a `min_lp_tokens_out` (or equivalent) field to `DepositRequest`** in `rs/sns/treasury_manager/treasury_manager.did`, so that the SNS governance proposal can encode a caller-specified minimum acceptable output.
2. **Propagate this field** through `construct_treasury_manager_deposit_payload` in `rs/sns/governance/src/extensions.rs` and enforce it in `execute_treasury_manager_deposit` by checking the returned `Balances` against the minimum.
3. **Require the field to be set** during `validate_deposit_operation` so that proposals without a slippage bound are rejected at validation time.
4. Alternatively, compute a slippage-aware minimum from an on-chain price oracle at proposal execution time and reject the deposit if the DEX quote falls below it.

---

### Proof of Concept

**Step 1 — Observe the adopted proposal.** An SNS governance proposal of type `ExecuteExtensionOperation { operation_name: "deposit", ... }` is adopted. Its execution is scheduled for the next heartbeat after the voting period ends. The execution time is publicly known.

**Step 2 — Manipulate the DEX pool.** Immediately before the heartbeat, an unprivileged attacker submits trades on the IC-native DEX (e.g., KongSwap) to shift the SNS/ICP pool ratio unfavorably (e.g., dump SNS tokens to lower the SNS price in the pool).

**Step 3 — Governance executes the deposit.** `perform_execute_extension_operation` → `execute_treasury_manager_deposit` is called. `construct_treasury_manager_deposit_payload` builds a `DepositRequest` with only `allowances` and no minimum output. The Treasury Manager calls the DEX `deposit` at the manipulated price. The SNS treasury receives fewer LP tokens than the deposited assets are worth at the pre-manipulation price.

**Step 4 — Attacker reverses the trade.** The attacker buys back SNS tokens at the now-lower price, profiting from the spread. The SNS treasury permanently holds LP tokens worth less than the deposited assets.

The root cause — no `min_amount_out` in `DepositRequest` and no output check in `execute_treasury_manager_deposit` — is confirmed by: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/sns/governance/src/extensions.rs (L1088-1098)
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
```

**File:** rs/sns/governance/src/extensions.rs (L1575-1609)
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
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
