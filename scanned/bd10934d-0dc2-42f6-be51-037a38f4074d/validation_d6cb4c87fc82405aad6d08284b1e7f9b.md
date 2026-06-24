### Title
SNS Treasury Valuation Manipulation via Inflated `icps_per_token` from Swap Derived State — (`rs/sns/governance/token_valuation/src/lib.rs`)

### Summary

The SNS governance treasury transfer limit system computes a real-time valuation of the SNS treasury by calling the swap canister's `get_derived_state` query to obtain `sns_tokens_per_icp`. This rate is derived from the **live, mutable** swap state during an open swap, not from a finalized, immutable price. An attacker who participates in an open SNS swap with a large ICP deposit can temporarily inflate `buyer_total_icp_e8s`, which **deflates** `sns_tokens_per_icp` (more ICP per token → fewer tokens per ICP → higher `icps_per_token` → higher treasury XDR valuation). A higher XDR valuation pushes the treasury into the "large" regime, which imposes a hard cap of 300,000 XDR on 7-day transfers instead of the more permissive "small" (no limit) or "medium" (25%) regimes. This is the inverse of the Notional attack: instead of forcing liquidations by inflating a rate, an attacker can **suppress legitimate SNS treasury proposals** by inflating the treasury's apparent XDR value, or conversely, by withdrawing participation at the right moment, deflate the valuation to the "small" regime to bypass transfer limits entirely.

### Finding Description

The `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` function in `rs/sns/governance/token_valuation/src/lib.rs` calls `get_derived_state` on the swap canister to obtain `sns_tokens_per_icp`:

```rust
call::<_, MyRuntime>(self.swap_canister_id, GetDerivedStateRequest {})
``` [1](#0-0) 

The swap canister's `derived_state()` computes `sns_tokens_per_icp` as:

```rust
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    .and_then(|d| d.to_f32())
    .unwrap_or(0.0);
``` [2](#0-1) 

This value is **live** — it changes with every ICP deposit or withdrawal during an open swap. The governance valuation code then inverts this to get `icps_per_token`:

```rust
let initial_icps_per_sns_token = Decimal::from(1)
    .checked_div(initial_sns_tokens_per_icp)
``` [3](#0-2) 

This `icps_per_token` feeds directly into the treasury XDR valuation:

```rust
tokens * icps_per_token * xdrs_per_icp
``` [4](#0-3) 

The valuation determines which transfer regime applies:

- ≤ 100,000 XDR → `NoLimit` (any amount can be transferred)
- ≤ 1,200,000 XDR → `Fraction(0.25)` (25% of treasury per 7 days)
- > 1,200,000 XDR → `Xdr(300_000)` (hard cap of 300,000 XDR per 7 days) [5](#0-4) 

The valuation is computed at **proposal submission time** and locked into the `ActionAuxiliary` stored with the proposal:

```rust
ActionAuxiliary::TransferSnsTreasuryFunds(valuation)
``` [6](#0-5) 

At execution time, the **same locked valuation** is reused to enforce the limit:

```rust
transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
    transfer,
    valuation?,
    ...
)
``` [7](#0-6) 

There is no `MAX_XDRS_PER_ICP` clamp — only a `MIN_XDRS_PER_ICP` floor of 1 XDR is enforced:

```rust
// Why Not Also Define MAX?
// Currently, we do not have/enforce a MAX_XDRS_PER_ICP
const MIN_XDRS_PER_ICP: Decimal = dec!(1);
``` [8](#0-7) 

### Impact Explanation

**Attack vector A — Suppressing treasury proposals (griefing):**
An attacker deposits a large amount of ICP into an open SNS swap just before a `TransferSnsTreasuryFunds` proposal is submitted. This inflates `buyer_total_icp_e8s`, deflates `sns_tokens_per_icp`, inflates `icps_per_token`, and pushes the treasury XDR valuation above 1,200,000 XDR. The proposal is then subject to the hard 300,000 XDR cap. If the SNS community intended to transfer more than 300,000 XDR worth of tokens in a 7-day window, the proposal is rejected at submission. The attacker can then withdraw their ICP from the swap (if the swap is still open or aborted), recovering their capital at near-zero cost.

**Attack vector B — Bypassing treasury limits (theft enablement):**
An attacker who controls a large SNS neuron majority can do the inverse: withdraw ICP from the swap (or time the attack when the swap has very little participation), deflating `buyer_total_icp_e8s`, inflating `sns_tokens_per_icp`, deflating `icps_per_token`, and pushing the treasury valuation below 100,000 XDR into the `NoLimit` regime. In this regime, **any amount** can be transferred from the treasury in a single proposal. The attacker then submits and passes a `TransferSnsTreasuryFunds` proposal to drain the treasury.

The impact is: **governance authorization bypass / ledger conservation bug** — the rate-limiting mechanism that protects SNS treasuries from rapid draining can be circumvented by manipulating the live swap state at proposal submission time.

### Likelihood Explanation

- The attack requires an open SNS swap (a time-limited window, but common during SNS launches).
- The attacker needs enough ICP to meaningfully shift `buyer_total_icp_e8s` relative to the swap's total participation — this is capital-intensive but recoverable.
- For Attack B, the attacker also needs a governance majority in the SNS, which is a significant prerequisite but not impossible for a malicious SNS founder or coordinated attacker.
- Attack A (griefing) requires no governance majority and is low-cost since ICP can be recovered after the swap ends or is aborted.
- The attack window is bounded by the swap lifecycle, but SNS swaps can last days to weeks.

### Recommendation

1. **Freeze the `sns_tokens_per_icp` price at swap finalization.** The swap canister already stores the final `buyer_total_icp_e8s` and `sns_token_e8s` after `finalize_swap`. The governance canister should read the finalized price from the committed swap state, not the live derived state.

2. **Add a `MAX_XDRS_PER_ICP` clamp** in `ProposalsAmountTotalUpperBound::clamp_xdrs_per_icp` (analogous to the existing `MIN_XDRS_PER_ICP`) to bound the maximum treasury valuation and prevent the `NoLimit` regime from being reached via price manipulation.

3. **Add a `MAX_ICPS_PER_TOKEN` clamp** in the valuation pipeline to prevent `icps_per_token` from being inflated to unrealistic values.

### Proof of Concept

1. An SNS swap is open with 1,000,000 SNS tokens available and 100,000 ICP deposited → `sns_tokens_per_icp = 10.0`, `icps_per_token = 0.1`.

2. Attacker deposits 9,900,000 ICP into the swap → `buyer_total_icp_e8s = 10,000,000`, `sns_tokens_per_icp = 0.1`, `icps_per_token = 10.0`.

3. SNS treasury holds 1,000,000 SNS tokens. Treasury XDR valuation = `1,000,000 tokens × 10 ICP/token × 10 XDR/ICP = 100,000,000 XDR` → regime: `Xdr(300_000)` hard cap.

4. A legitimate `TransferSnsTreasuryFunds` proposal for 500,000 SNS tokens (worth ~500,000 XDR at true price) is submitted. The inflated valuation caps the 7-day allowance at 300,000 XDR worth of tokens. The proposal is rejected at submission.

5. Attacker withdraws ICP after swap abort, recovering capital. The SNS governance is paralyzed for 7 days.

The root cause is in `fetch_icps_per_sns_token` at: [9](#0-8) 

which reads a live, manipulable query endpoint: [10](#0-9) 

to derive a price used in a security-critical treasury limit calculation: [11](#0-10)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L118-126)
```rust
    pub fn to_xdr(&self) -> Decimal {
        let Self {
            tokens,
            icps_per_token,
            xdrs_per_icp,
        } = self;

        tokens * icps_per_token * xdrs_per_icp
    }
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-365)
```rust
    async fn fetch_icps_per_sns_token(&self) -> Result<Decimal, ValuationError> {
        // (Concurrently) fetch the various pieces that we need to sythensize the result:
        let (get_derived_state_result, initial_supply_e8s_result, current_supply_result) = join!(
            // 1. SNS token price from swap.
            call::<_, MyRuntime>(self.swap_canister_id, GetDerivedStateRequest {}),
            // 2. Initial SNS token supply.
            initial_supply_e8s::<MyRuntime>(
                self.sns_token_ledger_canister_id,
                InitialSupplyOptions::new()
            ),
            // 3. Current SNS token supply.
            MyRuntime::call_with_cleanup::<_, (Nat,)>(
                self.sns_token_ledger_canister_id,
                "icrc1_total_supply",
                ()
            ),
        );
        // (Factors 2 and 3 tell us how much inflation there has been. For
        // example, if the amount of tokens has doubled since the beginning,
        // then the current ICPs per SNS token should be half of what it was at
        // the time of the swap.)

        // Unwrap (intermediate) results.
        let get_derived_state_response = get_derived_state_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain SNS token price at the time of the SNS initialization swap: {err:?}",
            ))
        })?;
        let initial_supply_e8s = initial_supply_e8s_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to determine the initial supply of SNS tokens: {err:?}",
            ))
        })?;
        let (current_supply_e8s,) = current_supply_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain the current supply of SNS tokens: {err:?}",
            ))
        })?;

        // Read the relevant fields.

        // Here, a floating point field is used. This is ok, because we are just
        // using this to come up with a valuation, which isn't an exact science.
        let initial_sns_tokens_per_icp: f64 = get_derived_state_response
            .sns_tokens_per_icp
            .ok_or_else(|| {
                ValuationError::new_mismatch(format!(
                    "Response from swap ({}) get_derived_state call did not \
                     contain sns_tokens_per_icp: {:#?}",
                    self.swap_canister_id, get_derived_state_response,
                ))
            })?;
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L389-394)
```rust
        let initial_icps_per_sns_token = Decimal::from(1)
            .checked_div(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
            ValuationError::new_arithmetic(format!(
                "Unable to perform 1 / sns_tokens_per_icp (where sns_tokens_per_icp = {initial_sns_tokens_per_icp}).",
            ))
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-114)
```rust
    fn in_tokens(mut valuation: Valuation) -> Result<Decimal, ProposalsAmountTotalLimitError> {
        Self::clamp_xdrs_per_icp(&mut valuation);

        let ValuationFactors {
            tokens: balance_tokens,
            icps_per_token,
            xdrs_per_icp,
        } = valuation.valuation_factors;

        let self_ = Self::from_valuation_xdr(valuation.to_xdr());
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

            Self::Fraction(fraction) => balance_tokens
                .checked_mul(fraction)
                // Overflow should not be possible, since fraction is supposed to be at most 1.0.
                .ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Unable to perform {balance_tokens} * {fraction}.",
                    ))
                })?,

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
        };

        Ok(result_tokens)
    }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L126-134)
```rust
        if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
            return Self::NoLimit;
        }

        if valuation_xdr <= Self::MAX_MEDIUM_TREASURY_SIZE_XDR {
            return Self::Fraction(ONE_QUARTER);
        }

        Self::Xdr(Self::MAX_XDR)
```

**File:** rs/sns/governance/src/proposal.rs (L591-594)
```rust
                Some(valuation) => Ok((
                    rendering,
                    ActionAuxiliary::TransferSnsTreasuryFunds(valuation),
                )),
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

**File:** rs/sns/swap/canister/canister.rs (L219-223)
```rust
#[query]
async fn get_derived_state(_request: GetDerivedStateRequest) -> GetDerivedStateResponse {
    log!(INFO, "get_derived_state");
    swap().derived_state().into()
}
```
