Audit Report

## Title
SNS Treasury Manager Deposit Executes Without Price/Slippage Bounds, Enabling DEX Price Manipulation to Drain SNS Treasury - (File: rs/sns/governance/src/extensions.rs)

## Summary

The SNS governance framework's `execute_treasury_manager_deposit` function grants ICRC-2 allowances and calls `deposit` on a DEX-backed treasury manager extension with no price, slippage, or minimum-output check at execution time. An attacker with sufficient DEX liquidity can manipulate the pool price during the multi-day governance voting window so that when the proposal executes, the treasury deposits at an extreme ratio and the attacker immediately drains the contributed funds with a counter-swap. The `DepositRequest` type carries only token amounts and owner accounts — no price bound field exists anywhere in the call path.

## Finding Description

**Validation path** (`validate_deposit_operation_impl`, lines 276–321 of `rs/sns/governance/src/extensions.rs`): The only check performed is that the requested SNS and ICP amounts each do not exceed 50% of the current treasury balance. No DEX pool price, pool ratio, or minimum-output bound is validated at proposal submission or at execution time.

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() { ... }
if icp_requested > icp_balance.checked_div(2).unwrap() { ... }
Ok(structurally_valid)
```

**Execution path** (`execute_treasury_manager_deposit`, lines 1546–1610): After re-running the same 50%-balance check via `validate_execute_extension_operation`, the function calls `approve_treasury_manager` (lines 1566–1573) to grant ICRC-2 allowances for the full `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`, then immediately calls `deposit` on the extension canister (line 1578) with no price guard.

**Payload construction** (`construct_treasury_manager_deposit_payload`, lines 1088–1099): The `DepositRequest` passed to the treasury manager contains only `allowances: Vec<Allowance>` — token amounts and refund accounts. The `DepositRequest` struct (confirmed in `rs/sns/treasury_manager/src/lib.rs` lines 285–287 and `rs/sns/treasury_manager/treasury_manager.did` lines 84–86) has no `min_output`, `max_slippage`, or `sqrt_price_limit` field.

**Codebase acknowledgment**: `rs/sns/treasury_manager/treasury_manager.did` lines 35–40 labels this a "Known Security Risk." The proposal-rendering code in `rs/sns/governance/src/proposal.rs` lines 1540–1545 warns voters that DEXes may lack slippage protection and that deposits are "vulnerable to front-running or sandwich attacks" — but this warning is informational only; no enforcement exists in the execution path.

**Current exploitability gate**: `ALLOWED_EXTENSIONS` (lines 48–54 of `extensions.rs`) is currently empty because KongSwap ceased operations on April 6, 2026. The vulnerability becomes live the moment any new treasury manager extension is whitelisted via NNS governance and an SNS DAO submits a `TreasuryManagerDeposit` proposal — both of which are the explicit intended use of this framework.

## Impact Explanation

Once a new extension is whitelisted, an attacker holding sufficient DEX liquidity can cause an SNS DAO to lose up to 50% of its ICP and SNS token treasury balances per proposal execution. The 50%-cap check (the only guard) limits per-proposal exposure but does not prevent the price-manipulation drain. For any SNS with a meaningful treasury this constitutes significant, concrete SNS governance asset loss. This matches the **High ($2,000–$10,000)** impact class: "Significant… SNS… security impact with concrete user or protocol harm" and "Unauthorized access to… governance assets… canister-controlled funds."

## Likelihood Explanation

- **Attacker capability**: Unprivileged; requires only sufficient DEX liquidity to move the pool price to an extreme tick. No admin key, governance majority, or subnet compromise is needed.
- **Timing window**: The IC has no public mempool, so traditional same-block frontrunning is impossible. However, SNS governance voting periods are multiple days long, giving the attacker ample time to execute the price-manipulation swap before proposal execution.
- **Preconditions**: The attack requires (a) a new treasury manager extension to be whitelisted by NNS governance and (b) an SNS DAO to submit a deposit proposal — both are the normal, intended operation of this framework. The integration test (`rs/nervous_system/integration_tests/tests/sns_extension_test.rs`, lines 454–457) confirms the full deposit→DEX flow including a second deposit at a different pool ratio, demonstrating the end-to-end path is production-ready.
- **Repeatability**: The attack can be repeated for each new `TreasuryManagerDeposit` proposal, up to the 50% cap each time.

## Recommendation

1. **Extend `DepositRequest`** (or the `ExtensionOperationArg` map) to include caller-specified minimum acceptable output amounts or a maximum acceptable slippage percentage, committed at proposal-submission time and stored in the proposal.
2. **Re-validate price bounds at execution time** inside `execute_treasury_manager_deposit`: query the DEX canister for the current pool price and reject the deposit if it falls outside the proposal-specified bounds before issuing the ICRC-2 approval.
3. Alternatively, require the treasury manager interface to expose a `min_output` parameter and have governance pass the proposal-time price as a hard floor in the `DepositRequest`.
4. At minimum, add a governance-layer check that queries the current pool price at execution time and aborts if it has deviated beyond a configurable threshold from the price at proposal submission.

## Proof of Concept

**Setup**: SNS treasury holds 100 ICP + 100 SNS. A `TreasuryManagerDeposit` proposal is submitted for 50 ICP + 50 SNS (within the 50% cap). A new treasury manager extension backed by a Uniswap-v3-style DEX is whitelisted.

**Attack** (during the multi-day voting window, before proposal execution):

```rust
// Attacker calls DEX canister directly (unprivileged):
dex_canister.swap(
    zero_for_one = true,           // sell SNS, buy ICP
    amount_specified = large_sns,
    sqrt_price_limit = MIN_SQRT_PRICE,  // push pool to extreme tick
);
// Pool is now ~100% ICP, ~0% SNS.

// Proposal executes:
// approve_treasury_manager grants 50 ICP + 50 SNS allowances
// treasury manager calls DEX deposit at current (manipulated) price
// Result: ~50 ICP deposited, ~0 SNS deposited (SNS returned as excess)

// Attacker counter-swaps:
dex_canister.swap(
    zero_for_one = false,          // sell tiny SNS, buy ICP
    amount_specified = 0.01_SNS,
    sqrt_price_limit = MAX_SQRT_PRICE,
);
// Attacker extracts ~50 ICP for 0.01 SNS cost.
```

**Reproducible test plan**: Write a PocketIC integration test extending `rs/nervous_system/integration_tests/tests/sns_extension_test.rs` that (1) submits a deposit proposal, (2) calls the mock DEX to push the pool price to an extreme tick before proposal execution, (3) executes the proposal, and (4) asserts that the attacker's ICP balance increased by approximately the deposited ICP amount while the treasury lost it. The existing test already demonstrates the second-deposit-at-different-ratio scenario (lines 454–457), confirming the mechanism is real.