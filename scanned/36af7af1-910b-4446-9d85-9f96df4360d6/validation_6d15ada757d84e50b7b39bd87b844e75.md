### Title
Unbounded `icps_per_token` in SNS Treasury Valuation Enables Governance-Controlled Limit Bypass - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
The SNS treasury transfer and mint-tokens proposal validation computes a treasury valuation using three factors: token balance, `icps_per_token`, and `xdrs_per_icp`. The `xdrs_per_icp` factor is clamped to a minimum of 1 XDR to prevent artificially low valuations from inflating the allowed transfer limit. However, the `icps_per_token` factor — derived from the SNS swap canister's `get_derived_state` — has **no upper bound clamp**. An SNS governance majority can inflate `icps_per_token` to an arbitrarily large value by minting SNS tokens (deflating the swap-derived price denominator), causing the treasury valuation to appear enormous, which pushes the treasury into the "large" regime and caps the 7-day transfer allowance at a fixed 300,000 XDR token-equivalent — but the token-equivalent amount is computed as `300_000 / xdrs_per_token`, where `xdrs_per_token = xdrs_per_icp * icps_per_token`. If `icps_per_token` is driven to near-zero (by inflating SNS token supply), the token-equivalent cap becomes astronomically large, allowing the treasury to be drained.

### Finding Description

The SNS treasury transfer limit is enforced in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` via `ProposalsAmountTotalUpperBound::in_tokens`. The valuation is computed in `rs/sns/governance/token_valuation/src/lib.rs` by `IcpsPerSnsTokenClient::fetch_icps_per_sns_token`, which derives `icps_per_token` as:

```
icps_per_token = (1 / sns_tokens_per_icp_at_swap) / (current_supply / initial_supply)
``` [1](#0-0) 

The `sns_tokens_per_icp` value comes from the SNS swap canister's `get_derived_state`, which computes it as:

```
sns_tokens_per_icp = tokens_available_for_swap / total_participant_icp_e8s
``` [2](#0-1) 

This is a **historical** value frozen at swap finalization. The `icps_per_token` is then scaled by the inflation ratio `current_supply / initial_supply`. If the SNS governance mints a large number of SNS tokens (via `MintSnsTokens` proposals), `current_supply` grows, `total_inflation` grows, and `icps_per_token` shrinks toward zero.

The limit computation in the "large treasury" branch is:

```rust
let xdrs_per_token = xdrs_per_icp * icps_per_token;
let tokens_per_xdr = xdrs_per_token.inv();
max_xdr.checked_mul(tokens_per_xdr)  // = 300_000 / xdrs_per_token
``` [3](#0-2) 

When `icps_per_token` → 0 (due to massive token minting), `xdrs_per_token` → 0, `tokens_per_xdr` → ∞, and the computed token cap becomes unbounded. The `xdrs_per_icp` has a `MIN_XDRS_PER_ICP = 1` floor: [4](#0-3) 

But there is **no corresponding `MIN_ICPS_PER_TOKEN` or `MAX_TOKENS_PER_XDR` clamp**. The code comment explicitly acknowledges the asymmetry:

> "Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our valuations to be in the 'large' regime, where actions are more limited." [5](#0-4) 

The same logic applies inversely to `icps_per_token`: there is no floor, so driving it to near-zero drives the token cap to near-infinity.

The valuation is fetched at proposal submission time and stored in `ActionAuxiliary`. At execution time, the stored valuation is reused: [6](#0-5) [7](#0-6) 

### Impact Explanation

An SNS governance majority can:
1. Submit and pass a `MintSnsTokens` proposal to massively inflate the SNS token supply, driving `icps_per_token` toward zero.
2. Submit a `TransferSnsTreasuryFunds` proposal while `icps_per_token` is near-zero. The treasury valuation in XDR becomes near-zero, placing the treasury in the "small" regime (`NoLimit`), or if the treasury is large enough in absolute token terms, the computed token cap `300_000 * tokens_per_xdr` becomes astronomically large.
3. The proposal passes the amount check and drains the entire SNS treasury (ICP or SNS tokens) in a single 7-day window.

**Impact:** An SNS governance majority can drain the entire SNS treasury beyond the intended 300,000 XDR / 7-day cap, bypassing the economic safety limit designed to protect token holders.

### Likelihood Explanation

This requires an SNS governance majority — a privileged role within the SNS. However, the threat model for SNS treasury limits explicitly includes protection against a governance majority acting maliciously or being captured. The limits exist precisely because governance majorities are not fully trusted with unrestricted treasury access. The attack is fully on-chain, requires no external dependencies, and can be executed in two sequential governance proposals within a single 7-day window.

**Likelihood:** Medium — requires governance majority, but that is the exact threat the treasury limits are designed to mitigate.

### Recommendation

Add a minimum floor for `icps_per_token` (analogous to `MIN_XDRS_PER_ICP`) in `ProposalsAmountTotalUpperBound::in_tokens`, or add a maximum cap on the computed `tokens_per_xdr` before multiplying by `max_xdr`. Specifically, in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`, add a `clamp_icps_per_token` function mirroring `clamp_xdrs_per_icp`, using a historically-grounded minimum (e.g., the minimum observed `icps_per_token` across all SNS launches). Alternatively, cap the final `tokens_per_xdr` result to prevent the computed allowance from exceeding the actual treasury balance.

### Proof of Concept

**Setup:** SNS with 1,000,000 SNS tokens in treasury, swap finalized at 10 SNS tokens per ICP (so `icps_per_token_initial = 0.1`), ICP at 10 XDR. Treasury XDR value = 1,000,000 × 0.1 × 10 = 1,000,000 XDR → "large" regime, cap = 300,000 XDR = 30,000 SNS tokens normally.

**Attack:**
1. Governance passes `MintSnsTokens` for 999,000,000,000 SNS tokens (1000x inflation). Now `total_inflation = 1,000,000,001,000 / 1,000,000 ≈ 1,000,000`. `icps_per_token = 0.1 / 1,000,000 = 0.0000001`.
2. Treasury XDR value = 1,000,000 × 0.0000001 × 10 = 0.001 XDR → "small" regime → `NoLimit`.
3. Governance submits `TransferSnsTreasuryFunds` for the entire 1,000,000 SNS token treasury. The `NoLimit` branch returns `balance_tokens` as the cap, so the full balance is allowed. [8](#0-7) 

The entire treasury is transferred in one proposal, bypassing the 300,000 XDR / 7-day limit entirely.

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L357-414)
```rust
        let initial_sns_tokens_per_icp: f64 = get_derived_state_response
            .sns_tokens_per_icp
            .ok_or_else(|| {
                ValuationError::new_mismatch(format!(
                    "Response from swap ({}) get_derived_state call did not \
                     contain sns_tokens_per_icp: {:#?}",
                    self.swap_canister_id, get_derived_state_response,
                ))
            })?;

        // Convert all numbers to Decimal.

        let initial_sns_tokens_per_icp = Decimal::from_f64_retain(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to convert sns_tokens_per_icp {initial_sns_tokens_per_icp} (double precision \
                     floating point) to Decimal.",
                ))
            })?;

        let initial_supply_e8s = i2d(initial_supply_e8s);

        let current_supply_e8s =
            Decimal::from(current_supply_e8s.0.to_u128().ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to convert current_supply_e8s ({current_supply_e8s}) from Nat to Decimal.",
                ))
            })?);

        // Do actual (simple) math.

        // Flip the ratio from SNS tokens per ICP to ICPs per SNS token.
        let initial_icps_per_sns_token = Decimal::from(1)
            .checked_div(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
            ValuationError::new_arithmetic(format!(
                "Unable to perform 1 / sns_tokens_per_icp (where sns_tokens_per_icp = {initial_sns_tokens_per_icp}).",
            ))
        })?;

        let total_inflation = current_supply_e8s
            .checked_div(initial_supply_e8s)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform current_supply / initial_supply \
                     (where current_supply_e8s = {current_supply_e8s} and initial_supply_e8s = {initial_supply_e8s})",
                ))
            })?;

        // Finally, current price = initial price scaled down by inflation (or deflation).
        initial_icps_per_sns_token
            .checked_div(total_inflation)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform initial_icps_per_sns_token / total_inflation \
                     (where initial_icps_per_sns_token = {initial_icps_per_sns_token} and total_inflation = {total_inflation})",
                ))
            })
```

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L60-64)
```rust
    /// # Why Not Also Define MAX?
    ///
    /// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
    /// valuations to be in the "large" regime, where actions are more limited.
    const MIN_XDRS_PER_ICP: Decimal = dec!(1);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L76-78)
```rust
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L88-110)
```rust
            Self::Xdr(max_xdr) => {
                let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "XDRs per token could not be calculated from valuation: {valuation:?}"
                    ))
                })?;

                // Calculate the inverse conversion rate.
                if xdrs_per_token == Decimal::from(0) {
                    // This is not reachable, because in this case, valuation.to_xdr() would return
                    // 0, and in that case, we would have taken the NoLimit branch.
                    return Err(ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "It appears that the tokens have zero value in XDR. valuation = {valuation:?}"
                    )));
                }
                let tokens_per_xdr = xdrs_per_token.inv();

                max_xdr.checked_mul(tokens_per_xdr).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Max tokens could not be calculated with valuation: {valuation:?}",
                    ))
                })?
            }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-140)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
    }
```

**File:** rs/sns/governance/src/governance.rs (L3000-3005)
```rust
        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2656)
```rust
pub(crate) fn transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err<'a>(
    transfer: &TransferSnsTreasuryFunds,
    valuation: Valuation,
    proposals: impl Iterator<Item = &'a ProposalData>,
    now_timestamp_seconds: u64,
) -> Result<(), GovernanceError> {
    let allowance_tokens = transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)
        .map_err(|err| {
            // This should not be possible, because valuation was already used the same way during
            // proposal submission/creation/validation.
            GovernanceError::new_with_message(
                ErrorType::InconsistentInternalData,
                format!(
                    "Unable to determined upper bound on the amount of \
                     TransferSnsTreasuryFunds proposals: {err:?}\nvaluation:{valuation:?}",
                ),
            )
        })?;

    // The total calculated here _could_ be different from what was calculated at proposal
    // submission/creation time. A difference would result from the execution of (another)
    // TransferSnsTreasuryFunds proposal between now and then.
    let spent_tokens = total_treasury_transfer_amount_tokens(
        proposals,
        transfer.from_treasury(),
        now_timestamp_seconds - 7 * ONE_DAY_SECONDS,
    )
    .map_err(|message| {
        GovernanceError::new_with_message(ErrorType::InconsistentInternalData, message)
    })?;

    let remainder_tokens = allowance_tokens - spent_tokens;
    let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)
        // This Err cannot be provoked, because we are dividing a u64 (amount_e8s) by a positive
        // integer (E8).
        .ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::UnreachableCode,
                format!(
                    "Unable to convert proposals amount {} e8s to tokens.",
                    transfer.amount_e8s,
                ),
            )
        })?;
    if transfer_amount_tokens > remainder_tokens {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Executing this proposal is not allowed at this time, because doing \
                 so would cause the 7 day upper bound of {allowance_tokens} tokens to be exceeded. \
                 Maybe, try again later? The total amount transferred in the past \
                 7 days stands at {spent_tokens} tokens, and the amount in this proposal is {transfer_amount_tokens} \
                 tokens. The upper bound is based on treasury valuation factors at \
                 the time of proposal submission: {valuation:?}",
            ),
        ));
    }
```
