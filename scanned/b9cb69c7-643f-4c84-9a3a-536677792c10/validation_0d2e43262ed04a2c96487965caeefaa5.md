### Title
Hardcoded `E8` Divisor in SNS Treasury Valuation Produces Incorrect Token Limits for Non-8-Decimal SNS Ledgers - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

`try_get_balance_valuation_factors` in the SNS governance token valuation library unconditionally divides the raw ICRC-1 balance by the constant `E8` (10^8) to produce a "whole token" count. The ICRC-1 standard permits any `u8` decimal value. If an SNS ledger is initialized with decimals ≠ 8, the `tokens` field of `ValuationFactors` is wrong, causing `ProposalsAmountTotalUpperBound::in_tokens` to compute incorrect 7-day treasury-transfer and mint-SNS-tokens limits.

---

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the function `try_get_balance_valuation_factors` converts the raw ICRC-1 balance to a "whole token" count by dividing by the hardcoded constant `E8 = 100_000_000`:

```rust
// line 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0)…?)
    / Decimal::from(E8);   // ← always 10^8, never queries icrc1_decimals
``` [1](#0-0) 

This same function is the single shared implementation for both ICP valuation (`try_get_icp_balance_valuation`) and SNS-token valuation (`try_get_sns_token_balance_valuation`): [2](#0-1) 

The `icps_per_token` value returned by `IcpsPerSnsTokenClient` is derived from the swap canister's `sns_tokens_per_icp`, which is itself computed as raw SNS base-units divided by raw ICP base-units:

```rust
// rs/sns/swap/src/swap.rs line 2992-2994
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    …
``` [3](#0-2) 

Because both `tokens` and `icps_per_token` carry the same decimal error in opposite directions, the final XDR product (`tokens × icps_per_token × xdrs_per_icp`) cancels and is numerically correct. However, the individual fields `tokens` and `icps_per_token` stored in `ValuationFactors` are each wrong by a factor of `10^(8 − actual_decimals)`.

`ProposalsAmountTotalUpperBound::in_tokens` uses these fields directly to compute the 7-day limit in token units:

```rust
// rs/sns/governance/proposals_amount_total_limit/src/lib.rs line 76-110
Self::Fraction(fraction) => balance_tokens.checked_mul(fraction)…,
Self::Xdr(max_xdr) => {
    let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token)…;
    max_xdr.checked_mul(tokens_per_xdr)…
}
``` [4](#0-3) 

**Concrete arithmetic for a 6-decimal SNS token (e.g., USDC-like):**

| Quantity | Correct value | Computed value | Error |
|---|---|---|---|
| `tokens` (1 whole token) | 1 | 0.01 | 100× under |
| `icps_per_token` | 1 ICP/token | 100 ICP/token | 100× over |
| XDR total | correct | correct | cancels |
| `Fraction` limit | `1 × f` | `0.01 × f` | 100× under |
| `Xdr` limit | `max_xdr / xdrs_per_token` | 100× smaller | 100× under |

**For an 18-decimal SNS token:**

| Quantity | Error |
|---|---|
| `tokens` | 10^10× over |
| `icps_per_token` | 10^10× under |
| `Fraction` / `Xdr` limit | 10^10× over → effectively unlimited |

---

### Impact Explanation

**Tokens with decimals > 8 (e.g., 18):** The computed `tokens` field is `10^(d−8)` times larger than the true whole-token count. Both the `Fraction` branch (limit = `balance_tokens × fraction`) and the `Xdr` branch (limit = `max_xdr / xdrs_per_token`) return a value `10^(d−8)` times larger than the correct limit. For 18 decimals this is a 10^10 multiplier, making the 7-day treasury-transfer and mint-SNS-tokens caps effectively unlimited. An SNS community could drain its entire treasury or mint unbounded tokens within a single 7-day window, bypassing the governance safety rails.

**Tokens with decimals < 8 (e.g., 6):** The limit is `10^(8−d)` times smaller than correct, blocking legitimate treasury proposals that are within the intended bounds. [5](#0-4) 

---

### Likelihood Explanation

All currently deployed SNS tokens use 8 decimals because the SNS framework historically assumed `e8s` throughout. However:

1. The ICRC-1 ledger used for SNS tokens accepts any `u8` decimal value at initialization; there is no on-chain enforcement that it must equal 8.
2. The NNS proposal to create an SNS (`CreateServiceNervousSystem`) passes arbitrary `LedgerInitArgs` including `decimals`; a proposer could set `decimals = 18` (or any value) and the NNS vote would not catch the downstream valuation bug.
3. Once the SNS is live, the SNS community (not the NNS) controls treasury proposals, so exploitation requires no further privileged access after creation.

Likelihood is **low** for existing SNS instances (all use 8 decimals today) but **non-zero** for future SNS deployments where the decimal field is set incorrectly, whether accidentally or intentionally.

---

### Recommendation

Replace the hardcoded `E8` divisor with a runtime query to `icrc1_decimals` on the ledger canister, and use `10^decimals` as the divisor:

```rust
// Fetch decimals alongside balance
let decimals = icrc1_client.icrc1_decimals().await?;
let divisor = Decimal::from(10u128.pow(decimals as u32));
let tokens = Decimal::from(raw_balance) / divisor;
```

Alternatively, add an explicit assertion during SNS initialization that the SNS ledger's `icrc1_decimals()` equals 8, consistent with the `e8s` naming convention used throughout the SNS framework.

---

### Proof of Concept

**Setup:** Deploy an SNS whose ICRC-1 ledger is initialized with `decimals = 18`. Fund the SNS treasury with 1 SNS token (= 10^18 base units).

**Execution:**

1. SNS governance calls `try_get_sns_token_balance_valuation` for the treasury account.
2. `icrc1_balance_of` returns `10^18`.
3. `tokens = 10^18 / 10^8 = 10^10` (should be `1`).
4. Swap's `sns_tokens_per_icp` = `10^18 / 10^8` = `10^10` (base-unit ratio).
5. `icps_per_token = 1 / 10^10` (should be `1 ICP/token` if swap price was 1 ICP/token).
6. XDR = `10^10 × 10^-10 × xdrs_per_icp` = correct.
7. `ProposalsAmountTotalUpperBound::in_tokens` enters `Fraction` branch: limit = `10^10 × fraction` instead of `1 × fraction`.
8. A `TransferSnsTreasuryFunds` proposal to transfer the entire treasury (1 token = 10^18 base units) passes the limit check, even though the correct limit would be a small fraction of 1 token. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L19-57)
```rust
pub async fn try_get_icp_balance_valuation(account: Account) -> Result<Valuation, ValuationError> {
    let timestamp = now();

    try_get_balance_valuation_factors(
        account,
        &mut LedgerCanister::<CdkRuntime>::new(ICP_LEDGER_CANISTER_ID),
        &mut IcpsPerIcpClient {},
        &mut new_standard_xdrs_per_icp_client::<CdkRuntime>(),
    )
    .await
    .map(|valuation_factors| Valuation {
        token: Token::Icp,
        account,
        timestamp,
        valuation_factors,
    })
}

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L141-191)
```rust
async fn try_get_balance_valuation_factors(
    account: Account,
    icrc1_client: &mut dyn Icrc1Client,
    icps_per_token_client: &mut dyn IcpsPerTokenClient,
    xdrs_per_icp_client: &mut dyn XdrsPerIcpClient,
) -> Result<ValuationFactors, ValuationError> {
    // Fetch the three ingredients:
    //
    //     1. balance
    //     2. token -> ICP
    //     3. ICP -> XDR
    //
    // No await here. Instead, we use join (right after this).
    let balance_of_request = icrc1_client.icrc1_balance_of(account);
    let icps_per_token_request = icps_per_token_client.get();
    let xdrs_per_icp_request = xdrs_per_icp_client.get();

    // Make all (3) requests (concurrently).
    let (balance_of_response, icps_per_token_response, xdrs_per_icp_response) = join!(
        balance_of_request,
        icps_per_token_request,
        xdrs_per_icp_request,
    );

    // Unwrap/forward errors to the caller.
    let balance_of_response = balance_of_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain balance from ledger: {err:?}"))
    })?;
    let icps_per_token_response = icps_per_token_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to determine ICPs per token: {err:?}"))
    })?;
    let xdrs_per_icp_response = xdrs_per_icp_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain XDR per ICP: {err:?}"))
    })?;

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

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-113)
```rust
impl ProposalsAmountTotalUpperBound {
    // A treasury can be small, medium, or large. These are the boundaries between those regimes.
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);

    /// A price quote less than this is considered "unrealistically" low. When that happens, we use
    /// this instead of the quoted value.
    ///
    /// # Motivation
    ///
    /// Low XDRs per ICP quotes would tend to cause our valuations to be in the "small" regime,
    /// where an SNS is allowed to take the biggest actions relative to their size. This is to
    /// minmize the damage caused by wacky price quotes.
    ///
    /// # What Value to Use
    ///
    /// Currently, the minimum XDRs per ICP used by NNS governance is 1. This is simply copied from
    /// there, specifically from the minimum_icp_xdr_rate field in NetworkEconomics.
    ///
    /// As of Mar 2024, the price of ICP is around 10 XDR. The lowest it has ever been is around 2.2
    /// XDR. FWIW, this is less than that.
    ///
    /// # Why Not Also Define MAX?
    ///
    /// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
    /// valuations to be in the "large" regime, where actions are more limited.
    const MIN_XDRS_PER_ICP: Decimal = dec!(1);

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
```
