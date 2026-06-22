### Title
SNS Treasury Transfer Limit Bypassed via Stale Genesis-Price Oracle for SNS Token Valuation — (`rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

The SNS governance treasury protection system uses a single, frozen genesis-swap price (adjusted only for token supply inflation) to value SNS tokens when enforcing the 7-day treasury transfer cap. This is directly analogous to the WBTC oracle issue: a bridged/wrapped asset is priced using a proxy price source that does not reflect current market conditions. When an SNS token has appreciated significantly since its genesis swap, the stale price undervalues the tokens, causing the hard XDR cap to be bypassed and allowing far more value to be drained from the treasury than the protocol intends.

---

### Finding Description

The `IcpsPerSnsTokenClient::fetch_icps_per_sns_token()` function in `rs/sns/governance/token_valuation/src/lib.rs` computes the current SNS token price as:

```
current_icps_per_token = (1 / genesis_sns_tokens_per_icp) / (current_supply / initial_supply)
``` [1](#0-0) 

The `genesis_sns_tokens_per_icp` is fetched from the swap canister's `get_derived_state` endpoint: [2](#0-1) 

After the SNS initialization swap finalizes, `participant_total_icp_e8s` and `tokens_available_for_swap` are frozen. The `derived_state()` function in the swap canister computes `sns_tokens_per_icp` purely from these frozen values: [3](#0-2) 

This means `genesis_sns_tokens_per_icp` is permanently fixed at the swap finalization price. The valuation model only adjusts for token supply inflation — it has **no mechanism to track current market price**. There is no secondary price source, no deviation threshold, and no staleness check.

This stale valuation is used directly to enforce the 7-day treasury transfer cap: [4](#0-3) 

The cap converts the XDR limit (300,000 XDR for large treasuries) into a token count using the stale price:

```
allowance_tokens = 300_000 XDR * (1 / stale_xdrs_per_token)
```

When the SNS token has appreciated 100× since genesis, `stale_xdrs_per_token` is 100× lower than the actual market price. The resulting `allowance_tokens` is therefore 100× larger than intended, meaning the governance can approve a transfer of tokens whose actual market value is 100× the intended 300,000 XDR cap — i.e., 30,000,000 XDR.

The `MIN_XDRS_PER_ICP` clamp only guards the ICP/XDR rate, not the SNS token price: [5](#0-4) 

The code explicitly documents that no `MAX_XDRS_PER_ICP` is enforced, but the analogous problem for `icps_per_token` is entirely unaddressed.

The valuation is snapshotted at proposal submission time and reused at execution time: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

For any SNS whose token has appreciated significantly since its genesis swap (a normal outcome for a successful project), the 7-day treasury transfer limit is effectively bypassed. A governance-adopted `TransferSnsTreasuryFunds` proposal can drain tokens whose actual XDR value is orders of magnitude above the intended 300,000 XDR hard cap. The same applies to `MintSnsTokens` proposals, which use the identical valuation path: [8](#0-7) 

The treasury protection mechanism — the primary on-chain safeguard against large SNS treasury drains — is rendered ineffective.

---

### Likelihood Explanation

Any SNS token that has appreciated since its genesis swap is affected. This is a realistic and expected outcome for successful SNS projects. The attacker path requires only that a `TransferSnsTreasuryFunds` proposal be submitted and adopted through normal governance — no privileged access, no key compromise, and no threshold attack is required. The proposal submitter is an ordinary neuron holder (unprivileged ingress sender). The limit check is supposed to be a hard cap that blocks even governance-approved proposals; the vulnerability defeats this cap.

---

### Recommendation

Replace the single genesis-price oracle with a dual-source approach analogous to the double oracle recommendation in the external report:

1. **Primary source**: Retain the inflation-adjusted genesis price as a floor/reference.
2. **Secondary source**: Integrate a current on-chain price source (e.g., a DEX TWAP or an ICP exchange-rate canister query for the SNS token if available).
3. **Conservative valuation**: Use the **higher** of the two prices when computing the treasury cap (i.e., assume the token is worth at least as much as the market says), so that the XDR cap is not bypassed when the token appreciates.
4. **Staleness guard**: Reject the genesis price if the swap canister's `get_derived_state` returns a price that is more than a configurable threshold below the secondary source.

At minimum, add a `MAX_ICPS_PER_TOKEN` clamp (analogous to the existing `MIN_XDRS_PER_ICP`) to bound how far the stale genesis price can diverge from a reasonable current estimate.

---

### Proof of Concept

**Setup**: SNS genesis swap finalized at 10 SNS tokens per ICP (i.e., 0.1 ICP per SNS token). Token supply has doubled since genesis (2× inflation). Current market price: 10 ICP per SNS token (100× appreciation). ICP/XDR rate: 10 XDR/ICP. Treasury holds 1,000,000 SNS tokens.

**Computed valuation** (using stale genesis price):
- `icps_per_token` = (1/10) / 2 = 0.05 ICP/token
- `xdrs_per_token` = 0.05 × 10 = 0.5 XDR/token
- Treasury XDR value = 1,000,000 × 0.5 = 500,000 XDR → "large" regime
- `allowance_tokens` = 300,000 / 0.5 = **600,000 tokens**

**Actual market value of allowance**:
- 600,000 tokens × 10 ICP/token × 10 XDR/ICP = **60,000,000 XDR**

**Intended cap**: 300,000 XDR. **Actual cap enforced**: 60,000,000 XDR — a **200× bypass**.

The attacker submits a `TransferSnsTreasuryFunds` proposal for 600,000 SNS tokens. The validation in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` passes because 600,000 ≤ 600,000. The proposal is adopted and executed, draining 60,000,000 XDR worth of tokens from the treasury. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L37-57)
```rust
pub async fn try_get_sns_token_balance_valuation(
    account: Account,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, ValuationError> {
    let timestamp = now();

    try_get_balance_valuation_factors(
        account,
        &mut LedgerCanister::<CdkRuntime>::new(sns_ledger_canister_id),
        &mut IcpsPerSnsTokenClient::<CdkRuntime>::new(swap_canister_id, sns_ledger_canister_id),
        &mut new_standard_xdrs_per_icp_client::<CdkRuntime>(),
    )
    .await
    .map(|valuation_factors| Valuation {
        token: Token::SnsToken,
        account,
        timestamp,
        valuation_factors,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-334)
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
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L386-415)
```rust
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
    }
```

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-140)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
    }
```

**File:** rs/sns/governance/src/proposal.rs (L570-578)
```rust
    // Validate amount. This requires calling CMC and the swap canister; hence, await.
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        transfer,
    )
    .await;
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
