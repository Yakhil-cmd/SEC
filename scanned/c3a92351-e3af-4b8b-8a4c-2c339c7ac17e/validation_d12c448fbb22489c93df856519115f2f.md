### Title
SNS TreasuryManager `deposit` Called With No Slippage Bounds, Enabling Sandwich Attacks on SNS Treasury Funds - (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS governance extension framework executes treasury deposits into DEX liquidity pools via `execute_treasury_manager_deposit`, which calls the TreasuryManager canister's `deposit` method with a `DepositRequest` containing only token allowances and no minimum LP tokens out (no slippage bound). The `DepositRequest` type itself has no slippage field. The codebase explicitly acknowledges this as a "Known Security Risk" but provides no enforcement mechanism, leaving SNS treasury funds exposed to sandwich attacks at deposit time.

---

### Finding Description

When an SNS governance proposal of type `ExecuteExtensionOperation` with `operation_name = "deposit"` is adopted and executed, the function `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` is called. It constructs a `DepositRequest` via `construct_treasury_manager_deposit_payload`, which only populates the `allowances` field (token amounts and owner accounts), and then calls `deposit` on the TreasuryManager extension canister with no minimum LP tokens out parameter.

The `DepositRequest` type defined in `rs/sns/treasury_manager/treasury_manager.did` is:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

There is no `min_lp_tokens_out`, `min_amount_out`, or any slippage bound field. The `construct_treasury_manager_deposit_payload` function builds this request with only the token allowances:

```rust
fn construct_treasury_manager_deposit_payload(...) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;
    let arg = DepositRequest { allowances };
    ...
}
```

The codebase itself acknowledges the risk in two places:

1. `rs/sns/treasury_manager/treasury_manager.did` lines 35–40:
   > "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."

2. `rs/sns/governance/src/proposal.rs` lines 1540–1545 (only a text warning in proposal rendering, not an enforcement):
   > "Some Decentralized Exchanges lack slippage protection during deposits... This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks."

Despite acknowledging the risk, neither the `DepositRequest` API nor the governance execution path enforces any minimum LP tokens received.

---

### Impact Explanation

An attacker who can observe when a `TreasuryManagerDeposit` governance proposal is about to be executed (all SNS proposals and their voting deadlines are public on-chain) can manipulate the DEX pool state before the deposit executes. Because the deposit carries no minimum LP tokens out, the TreasuryManager will accept any ratio of LP tokens returned by the DEX, no matter how unfavorable. The SNS treasury (holding real ICP and SNS tokens) can lose a significant portion of deposited value to the attacker. The attacker profits by sandwiching the deposit: skewing the pool price before the deposit, then restoring it after, extracting value from the SNS treasury.

---

### Likelihood Explanation

SNS governance proposals are fully public, including their execution timing. Any canister or principal that can interact with the same DEX can observe the proposal's voting deadline and execute a sandwich. The IC's deterministic execution model means that while there is no traditional mempool, the execution of a proposal after its voting period is predictable to the block. A malicious DEX or a canister that can call the DEX in the same or preceding round can manipulate the pool. The codebase itself treats this as a known, unmitigated risk.

---

### Recommendation

1. Add a `min_lp_tokens_out: opt nat` field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did` and `rs/sns/treasury_manager/src/lib.rs`.
2. Extend `ValidatedDepositOperationArg` in `rs/sns/governance/src/extensions.rs` to include a `min_lp_tokens_out` field parsed from the governance proposal payload.
3. Pass `min_lp_tokens_out` through `construct_treasury_manager_deposit_payload` into the `DepositRequest` sent to the TreasuryManager canister.
4. Require TreasuryManager implementations to enforce this bound when calling the DEX, rejecting the deposit if the returned LP tokens are below the minimum.
5. Validate in `validate_deposit_operation_impl` that `min_lp_tokens_out` is present and non-zero.

---

### Proof of Concept

1. An SNS DAO adopts a `ExecuteExtensionOperation` proposal with `operation_name = "deposit"` and `treasury_allocation_sns_e8s = X`, `treasury_allocation_icp_e8s = Y`.
2. The proposal voting period ends; execution is imminent and publicly observable.
3. An attacker calls the DEX to heavily skew the SNS/ICP pool price (e.g., dumps SNS tokens into the pool, making SNS cheap relative to ICP).
4. `execute_treasury_manager_deposit` fires, calling `deposit` with `DepositRequest { allowances: [X SNS, Y ICP] }` — no minimum LP tokens out.
5. The TreasuryManager deposits into the DEX at the manipulated price, receiving far fewer LP tokens than the fair-price equivalent.
6. The attacker reverses their trade, profiting from the spread. The SNS treasury has permanently lost value.

The root cause is confirmed at: [1](#0-0) [2](#0-1) [3](#0-2) 

The acknowledged but unenforced risk is documented at: [4](#0-3) [5](#0-4)

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

**File:** rs/sns/governance/src/extensions.rs (L1575-1579)
```rust
    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
