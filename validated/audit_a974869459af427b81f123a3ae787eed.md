Audit Report

## Title
SNS Treasury 7-Day Transfer Cap Bypassed via Stale Genesis-Swap Price Oracle — (`rs/sns/governance/token_valuation/src/lib.rs`)

## Summary

The SNS governance treasury protection system computes SNS token value using only the genesis-swap price adjusted for supply inflation, with no mechanism to track current market price. When an SNS token has appreciated significantly since its genesis swap, the stale price undervalues the tokens, causing the 7-day hard XDR cap (`MAX_XDR = 300,000`) to be bypassed. A governance-adopted `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal can drain tokens whose actual market value is orders of magnitude above the intended cap.

## Finding Description

**Root cause — stale price oracle in `IcpsPerSnsTokenClient::fetch_icps_per_sns_token`:**

`rs/sns/governance/token_valuation/src/lib.rs` lines 314–415 compute the current SNS token price as:

```
current_icps_per_token = (1 / genesis_sns_tokens_per_icp) / (current_supply / initial_supply)
```

The `genesis_sns_tokens_per_icp` is fetched from the swap canister's `get_derived_state` endpoint (line 318). The swap canister's `derived_state()` at `rs/sns/swap/src/swap.rs` lines 2992–2995 computes `sns_tokens_per_icp` purely from `tokens_available_for_swap` and `participant_total_icp_e8s`:

```rust
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    .and_then(|d| d.to_f32())
    .unwrap_or(0.0);
```

After the SNS initialization swap finalizes, both `tokens_available_for_swap` and `participant_total_icp_e8s` are frozen. Therefore `genesis_sns_tokens_per_icp` is permanently fixed at the swap finalization price. The valuation model only adjusts for token supply inflation — it has no mechanism to track current market price, no secondary price source, no deviation threshold, and no staleness check.

**Cap enforcement uses the stale price:**

`rs/sns/governance/proposals_amount_total_limit/src/lib.rs` lines 88–110 convert the XDR limit into a token count using the stale price:

```rust
let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token)...;
let tokens_per_xdr = xdrs_per_token.inv();
max_xdr.checked_mul(tokens_per_xdr)  // MAX_XDR = 300_000
```

When `icps_per_token` is 100× lower than market (due to 100× appreciation), `allowance_tokens` is 100× larger than intended.

**The `MIN_XDRS_PER_ICP` clamp is insufficient:**

Lines 137–140 clamp only `xdrs_per_icp`, not `icps_per_token`. The code comment at lines 60–63 explicitly acknowledges no `MAX_XDRS_PER_ICP` is enforced, but the analogous problem for `icps_per_token` is entirely unaddressed.

**Valuation is snapshotted at proposal submission and reused at execution:**

`rs/sns/governance/src/proposal.rs` lines 570–578 capture the valuation at proposal submission time. `rs/sns/governance/src/governance.rs` lines 3000–3005 reuse this snapshotted valuation at execution time via `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. Both checks use the same stale price.

## Impact Explanation

The treasury transfer limit (`MAX_XDR = 300,000`) is a protocol-level hard cap specifically designed to block even governance-approved proposals from draining the treasury beyond a safe threshold. This is not a standard governance action — it is a safeguard against governance majority attacks. The code bug (stale oracle) defeats this safeguard, allowing a governance-adopted proposal to drain SNS treasury tokens whose actual market value is orders of magnitude above the intended 300,000 XDR cap. For a token with 100× appreciation and a 1,000,000-token treasury, the PoC demonstrates a 200× bypass (60,000,000 XDR drained vs. 300,000 XDR intended), well above the $1M threshold for Critical impact. This maps to: **High–Critical: Significant SNS governance/treasury security impact with concrete protocol harm; theft of canister-controlled funds exceeding $1M under realistic conditions.**

## Likelihood Explanation

Any SNS token that has appreciated since its genesis swap is affected — a normal and expected outcome for successful SNS projects. The attacker requires governance majority for the SNS in question (a meaningful constraint, placing this at High rather than unconditional Critical), but no privileged system access, no key compromise, and no threshold attack. The proposal is submitted and adopted through normal governance. The limit check is supposed to be a hard cap that blocks even governance-approved proposals; the vulnerability defeats this cap for any SNS with token appreciation.

## Recommendation

1. **Primary fix**: Replace the single genesis-price oracle with a dual-source approach. Retain the inflation-adjusted genesis price as a reference, and integrate a current on-chain price source (e.g., a DEX TWAP or ICP exchange-rate canister query for the SNS token).
2. **Conservative valuation**: Use the **higher** of the two prices when computing the treasury cap (assume the token is worth at least as much as the market says), so the XDR cap is not bypassed when the token appreciates.
3. **Minimum fix**: Add a `MAX_ICPS_PER_TOKEN` clamp in `clamp_xdrs_per_icp` (or a parallel `clamp_icps_per_token` function) analogous to the existing `MIN_XDRS_PER_ICP`, to bound how far the stale genesis price can diverge from a reasonable current estimate.
4. **Staleness guard**: Reject the genesis price if the swap canister's `get_derived_state` returns a price that is more than a configurable threshold below a secondary source.

## Proof of Concept

**Setup**: Genesis swap at 10 SNS tokens/ICP. Supply doubled since genesis (2× inflation). Current market: 10 ICP/SNS token (100× appreciation). ICP/XDR rate: 10 XDR/ICP. Treasury: 1,000,000 SNS tokens.

**Computed valuation (stale genesis price)**:
- `icps_per_token` = (1/10) / 2 = 0.05 ICP/token
- `xdrs_per_token` = 0.05 × 10 = 0.5 XDR/token
- Treasury XDR value = 1,000,000 × 0.5 = 500,000 XDR → "large" regime → `MAX_XDR = 300,000` applies
- `allowance_tokens` = 300,000 / 0.5 = **600,000 tokens**

**Actual market value of allowance**:
- 600,000 × 10 ICP/token × 10 XDR/ICP = **60,000,000 XDR**

**Intended cap**: 300,000 XDR. **Actual cap enforced**: 60,000,000 XDR — a **200× bypass**.

**Reproducible test plan**: Write a unit test in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` (or `token_valuation/src/lib.rs`) that mocks `IcpsPerSnsTokenClient` to return a genesis-era `icps_per_token` of 0.05 (reflecting 100× appreciation), sets `xdrs_per_icp` to 10, and calls `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` with a treasury valuation of 500,000 XDR. Assert that `allowance_tokens` = 600,000, then assert that 600,000 tokens × 10 ICP × 10 XDR = 60,000,000 XDR >> 300,000 XDR, demonstrating the bypass.