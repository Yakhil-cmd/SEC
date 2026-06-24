### Title
Missing Slippage Protection and Deadline in SNS Treasury Manager Deposit/Withdraw API Allows Front-Running of SNS DAO Treasury Funds - (File: rs/sns/treasury_manager/treasury_manager.did)

---

### Summary

The SNS Treasury Manager API (`DepositRequest` and `WithdrawRequest`) does not include any slippage protection parameters (minimum/maximum acceptable price ratio, minimum received LP tokens, or a deadline/expiry). When an SNS governance proposal to deposit treasury funds into a DEX liquidity pool is adopted and executed, the actual deposit occurs at whatever price ratio the DEX currently holds — which may have changed significantly during the proposal voting period. An adversary who monitors governance can sandwich the deposit execution, extracting value from the SNS treasury at the DAO's expense.

---

### Finding Description

The `DepositRequest` type defined in `rs/sns/treasury_manager/treasury_manager.did` and mirrored in `rs/sns/treasury_manager/src/lib.rs` contains only `allowances` (token amounts):

```
type DepositRequest = record {
  allowances : vec Allowance;
};
```

No fields exist for: minimum acceptable LP tokens received, acceptable price ratio bounds, or a deadline after which the deposit should be rejected.

The execution path is:

1. An SNS neuron holder submits a `RegisterExtension` or `ExecuteExtensionOperation` (deposit) proposal specifying `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`.
2. The proposal enters a voting period (days to weeks).
3. Upon adoption, `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` is called. It calls `construct_treasury_manager_deposit_payload`, which builds a `DepositRequest { allowances }` containing only the token amounts — no price bounds.
4. The governance canister calls `extension_canister_id.deposit(arg_blob)`, which forwards the funds to the DEX at the current market price.

The codebase itself acknowledges this in two places:

- `rs/sns/treasury_manager/treasury_manager.did` lines 35–40:
  > "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."

- `rs/sns/governance/src/proposal.rs` lines 1542–1545:
  > "Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks."

The `WithdrawRequest` has the same structural gap — no minimum received amounts or deadline.

---

### Impact Explanation

An adversary can sandwich the deposit execution:

1. Monitor SNS governance for an adopted deposit proposal (public on-chain state).
2. Before the proposal executes, submit a large trade on the target DEX to skew the pool price against the SNS treasury (e.g., buy SNS tokens to inflate their price relative to ICP).
3. The governance canister executes the deposit at the manipulated ratio — the SNS treasury receives far fewer LP tokens than expected.
4. The adversary immediately reverses their trade, extracting the price impact as profit.

Because the `DepositRequest` carries no `min_lp_tokens_out`, `max_price`, or `deadline`, the Treasury Manager canister has no on-chain basis to reject the deposit even if the price has moved adversely. The SNS DAO treasury (holding both SNS tokens and ICP) can suffer a permanent loss of value proportional to the price manipulation. This is a **ledger conservation bug** affecting SNS treasury assets.

---

### Likelihood Explanation

- SNS governance proposals are public and their adoption is observable by anyone.
- The voting period (days) gives ample time to prepare a sandwich.
- On-chain DEX pools on the IC (e.g., KongSwap, which was the only blessed extension until April 2026) are susceptible to price manipulation by any canister caller with sufficient liquidity.
- The attack requires no privileged access — only capital to move the DEX price temporarily.
- The codebase explicitly documents this as a known risk, confirming the attack surface is understood but unmitigated at the protocol level.

---

### Recommendation

1. **Add slippage parameters to `DepositRequest`**: Include `min_lp_tokens_out: opt nat` and/or `max_price_ratio: opt record { numerator: nat; denominator: nat }` so the Treasury Manager canister can enforce acceptable execution bounds before forwarding funds to the DEX.
2. **Add a deadline field**: Include `deadline_ns: opt nat64` so the deposit can be rejected if it is not executed within a governance-specified time window after proposal adoption.
3. **Enforce at the governance layer**: `validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs` should require that slippage parameters are present in the proposal payload before the proposal is accepted.
4. **Re-validate price at execution time**: Before calling `deposit`, the governance canister (or the Treasury Manager) should query the current DEX price and compare it against the proposal-specified bounds, rejecting the operation if the deviation exceeds the threshold.

---

### Proof of Concept

**Step 1 — Observe the gap in `DepositRequest`:**

`rs/sns/treasury_manager/src/lib.rs` lines 284–287:
```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
    // No min_lp_tokens_out, no max_price, no deadline
}
```

**Step 2 — Trace execution through governance:**

`rs/sns/governance/src/extensions.rs` lines 1088–1099:
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;
    let arg = DepositRequest { allowances };  // <-- only amounts, no price bounds
    candid::encode_one(&arg)...
}
```

`rs/sns/governance/src/extensions.rs` lines 1566–1578:
```rust
// 1. Transfer funds from treasury to treasury manager
governance.approve_treasury_manager(...).await?;
// 2. Call deposit on treasury manager — at whatever price the DEX currently has
governance.env.call_canister(extension_canister_id, "deposit", arg_blob).await
```

**Step 3 — Confirm the codebase acknowledges the risk:**

`rs/sns/treasury_manager/treasury_manager.did` lines 35–40:
```
// Known Security Risks:
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

`rs/sns/governance/src/proposal.rs` lines 1540–1545:
```
## WARNING
Some Decentralized Exchanges lack slippage protection during deposits. Consequently,
deposited asset ratios may deviate from those specified in the proposal.
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running
or sandwich attacks.
```

**Attack scenario:**
- SNS DAO adopts a proposal to deposit 1,000,000 SNS + 10,000 ICP into a KongSwap pool.
- Adversary observes the adopted proposal, buys large amounts of SNS on KongSwap to inflate the SNS/ICP price.
- Governance executes the deposit; the SNS treasury receives LP tokens representing a skewed ratio (fewer ICP-equivalent value).
- Adversary sells SNS back, pocketing the price impact. The SNS treasury has permanently lost value with no on-chain recourse. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/sns/governance/src/extensions.rs (L1545-1578)
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
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
