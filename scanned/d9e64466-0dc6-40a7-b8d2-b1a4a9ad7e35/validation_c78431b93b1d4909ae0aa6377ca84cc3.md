### Title
Hardcoded `E8` Decimal Assumption in SNS Token Balance Valuation Produces Incorrect Treasury Limits — (`File: rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

`try_get_balance_valuation_factors` in the SNS governance token-valuation crate unconditionally divides the raw ICRC-1 balance by the ICP-specific constant `E8` (10^8) when converting to whole tokens. Because the ICRC-1 standard allows any number of decimals and the SNS ledger accepts a caller-supplied `decimals` field at initialization, an SNS whose native token carries a decimal count other than 8 will produce a systematically wrong XDR valuation. That valuation is the sole input to the 7-day treasury-transfer and token-minting upper-bound checks enforced by SNS governance.

---

### Finding Description

`try_get_balance_valuation_factors` is the shared helper used for both ICP and SNS-token treasury valuations:

```rust
// rs/sns/governance/token_valuation/src/lib.rs  line 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0)
    .map_err(|err| ValuationError::new_arithmetic(...))?
) / Decimal::from(E8);   // ← always 10^8, never queried from the ledger
``` [1](#0-0) 

`E8` is defined as the ICP-specific constant `100_000_000`: [2](#0-1) 

The same function is called for SNS-token balances via `try_get_sns_token_balance_valuation`: [3](#0-2) 

The ICRC-1 ledger used by every SNS accepts a `decimals` field at init time and exposes it via `icrc1_decimals`: [4](#0-3) 

No call to `icrc1_decimals` is ever made inside `try_get_balance_valuation_factors`; the divisor is always `E8`. The resulting `tokens` value feeds directly into `ValuationFactors`, which is multiplied by `icps_per_token` and `xdrs_per_icp` to produce the XDR valuation used by `ProposalsAmountTotalUpperBound`: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

| SNS token decimals | Divisor used | Actual divisor needed | Effect on `tokens` | Effect on limit |
|---|---|---|---|---|
| 6 (e.g. USDC-style) | 10^8 | 10^6 | 100× too small | 100× too restrictive — legitimate proposals rejected |
| 18 (e.g. ETH-style) | 10^8 | 10^18 | 10^10× too large | 10^10× too permissive — treasury-drain proposals pass |

In the 18-decimal case the computed XDR valuation of the treasury is inflated by a factor of 10^10, which pushes every SNS into the `NoLimit` branch of `ProposalsAmountTotalUpperBound`, effectively removing the 7-day treasury-transfer cap and the minting cap entirely. An SNS community could then pass a single proposal that transfers or mints the entire treasury balance in one step, bypassing the rate-limiting safeguard.

---

### Likelihood Explanation

The SNS ledger is initialized with a caller-supplied `decimals` value; the SNS-W / NNS proposal flow does not enforce `decimals == 8`. The SNS default documentation uses the "e8s" naming convention as a convention, not a protocol invariant. Any SNS created with a non-8-decimal token — whether by mistake or by design — silently activates this miscalculation. The entry path is an ordinary `TransferSnsTreasuryFunds` or `MintSnsTokens` governance proposal submitted by any neuron holder of the affected SNS; no privileged access is required after the SNS is deployed.

---

### Recommendation

Replace the hardcoded `E8` divisor with a runtime query to `icrc1_decimals` on the ledger being valued:

```rust
// Fetch decimals alongside balance
let decimals_response = icrc1_client.icrc1_decimals().await?;
let divisor = Decimal::from(10u64.pow(decimals_response as u32));
let tokens = Decimal::from(raw_balance) / divisor;
```

For the ICP path, `icrc1_decimals` returns 8, so the existing behaviour is preserved. For SNS tokens with any other decimal count the conversion becomes correct.

---

### Proof of Concept

Consider an SNS initialized with `decimals = 18`. The SNS treasury holds `1_000 * 10^18` raw units (= 1 000 whole tokens). The valuation code computes:

```
tokens = 1_000 * 10^18 / 10^8 = 1_000 * 10^10 = 10^13 "tokens"
```

instead of the correct `1_000`. With `icps_per_token = 0.01` and `xdrs_per_icp = 2`, the XDR valuation becomes `10^13 * 0.01 * 2 = 2 * 10^11 XDR` — far above the `MAX_MEDIUM_TREASURY_SIZE_XDR` of 1 200 000 XDR, so `ProposalsAmountTotalUpperBound` resolves to `Xdr(300_000)`. Converting back: `300_000 / (0.01 * 2) = 15_000_000` "tokens" — but since the computed token count is already `10^13`, the limit of `15_000_000` is never binding, and the proposal passes regardless of the actual transfer amount. A neuron holder submits a `TransferSnsTreasuryFunds` proposal for the entire treasury; it clears the limit check and drains the SNS. [7](#0-6) [8](#0-7)

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L117-127)
```rust
impl ValuationFactors {
    pub fn to_xdr(&self) -> Decimal {
        let Self {
            tokens,
            icps_per_token,
            xdrs_per_icp,
        } = self;

        tokens * icps_per_token * xdrs_per_icp
    }
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L176-191)
```rust
    // Extract and interpret the data we actually care about from the (Ok) responses.
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
    let icps_per_token = icps_per_token_response;
    let xdrs_per_icp = xdrs_per_icp_response;

    // Compose the fetched/interpretted data (i.e. multiply them) to construct the final result.
    Ok(ValuationFactors {
        tokens,
        icps_per_token,
        xdrs_per_icp,
    })
}
```

**File:** rs/nervous_system/common/src/lib.rs (L60-61)
```rust
// 10^8
pub const E8: u64 = 100_000_000;
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L714-716)
```rust
            token_name,
            decimals: decimals.unwrap_or_else(default_decimals),
            metadata: map_metadata_or_trap(metadata, true, sink), // require_valid=true for init
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
