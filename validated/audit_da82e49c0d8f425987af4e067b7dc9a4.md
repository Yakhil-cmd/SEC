### Title
Missing Slippage Protection in `DepositRequest` Enables Front-Running of SNS Treasury DEX Deposits - (File: rs/sns/treasury_manager/treasury_manager.did)

---

### Summary

The `DepositRequest` type in the Treasury Manager API contains no minimum-received-amount (slippage) parameters. When `execute_treasury_manager_deposit` in SNS governance executes a governance-approved deposit, it approves ICRC-2 allowances and calls `deposit` on the treasury manager canister with no minimum LP token or minimum asset-ratio guarantee. The returned `Balances` are only logged, never validated against any minimum expected return. This is structurally identical to the Uniswap `addLiquidity(amountAMin=0, amountBMin=0)` pattern described in the reference report.

---

### Finding Description

The `DepositRequest` struct in both the Candid interface and the Rust library contains only `allowances`:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

There is no `min_lp_tokens`, `min_asset_a`, `min_asset_b`, or any equivalent slippage-protection field.

The `treasury_manager.did` itself explicitly acknowledges this gap under **Known Security Risks**:

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved.

The execution path in `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`) is:

1. Approve the treasury manager canister with ICRC-2 allowances for the exact `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` amounts voted on by the DAO.
2. Call `deposit` on the treasury manager with only the `allowances` — no minimum received amount.
3. Receive a `Balances` response, which is only logged (`log!(INFO, "TreasuryManager.deposit succeeded with response: {:?}", balances)`), never compared against any minimum expected value.

Because the `DepositRequest` API structurally has no slippage field, no treasury manager implementation — regardless of how carefully written — can receive a minimum LP token guarantee from the SNS governance caller. The governance canister cannot express "accept this deposit only if we receive at least X LP tokens."

The `validate_and_render_register_extension` function in `rs/sns/governance/src/proposal.rs` includes a warning acknowledging the attack surface:

> Some Decentralized Exchanges lack slippage protection during deposits … making them vulnerable to front-running or sandwich attacks.

Yet neither the API nor the execution logic enforces any mitigation.

---

### Impact Explanation

An SNS DAO that uses a treasury manager to deposit SNS tokens and ICP into a DEX liquidity pool can have its deposit front-run. A front-runner observes the pending governance execution (all IC canister calls are observable), manipulates the DEX pool price before the deposit lands, and causes the treasury manager to deposit at a worse ratio. The SNS treasury receives significantly fewer LP tokens than the DAO voted to accept. The financial loss is permanent and proportional to the size of the deposit and the severity of the price manipulation. Because the `Balances` response is never validated, the governance execution reports success regardless of how few LP tokens were actually received.

---

### Likelihood Explanation

IC canister update calls are publicly observable before execution. Any party monitoring the IC mempool or governance execution queue can detect a pending `execute_treasury_manager_deposit` call and front-run it on the target DEX. This is a standard sandwich attack requiring no privileged access — only the ability to submit transactions to the same DEX before and after the deposit. The attack is economically rational whenever the deposit size is large enough to move the pool price.

---

### Recommendation

1. Add slippage protection fields to `DepositRequest`:
   ```candid
   type DepositRequest = record {
     allowances : vec Allowance;
     min_lp_tokens : opt nat;          // minimum LP tokens to accept
     min_asset_amounts : opt vec record { principal; nat }; // per-asset minimums
   };
   ```
2. In `execute_treasury_manager_deposit`, validate the returned `Balances` against a minimum expected LP token amount computed at proposal submission time (e.g., using a TWAP oracle or a governance-specified slippage tolerance).
3. If the returned balances fall below the minimum, treat the deposit as failed and revert the ICRC-2 approvals.

---

### Proof of Concept

**Root cause — `DepositRequest` has no slippage field:** [1](#0-0) 

**Acknowledged as a Known Security Risk in the same file:** [2](#0-1) 

**Rust struct mirrors the same gap:** [3](#0-2) 

**`execute_treasury_manager_deposit` approves tokens and calls `deposit` with no minimum received amount:** [4](#0-3) 

**Returned `Balances` are only logged, never validated:** [5](#0-4) 

**Governance proposal renderer acknowledges front-running risk but provides no enforcement:** [6](#0-5) 

**Attack flow:**
1. SNS DAO passes a `ExecuteExtensionOperation` deposit proposal specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y`.
2. On execution, `approve_treasury_manager` grants ICRC-2 allowances for exactly `X` SNS tokens and `Y` ICP to the treasury manager canister.
3. `call_canister(extension_canister_id, "deposit", arg_blob)` is issued with a `DepositRequest` containing only the allowances — no minimum LP token field.
4. A front-runner observes the pending call, buys one asset on the DEX to skew the pool ratio, causing the treasury manager to deposit at a worse price.
5. The treasury manager deposits all approved tokens but receives far fewer LP tokens than the DAO expected.
6. The `Balances` response is logged as success; the governance proposal is marked `Executed`.
7. The front-runner sells back their position, extracting value from the SNS treasury.

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

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1578)
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
```

**File:** rs/sns/governance/src/extensions.rs (L1603-1607)
```rust
    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );
```

**File:** rs/sns/governance/src/proposal.rs (L1541-1546)
```rust

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

```
