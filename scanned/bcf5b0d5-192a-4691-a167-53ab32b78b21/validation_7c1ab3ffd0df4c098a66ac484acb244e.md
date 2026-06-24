### Title
Hardcoded `E8` Decimal Assumption in SNS Treasury Valuation Bypasses Governance Transfer Limits - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

The SNS governance treasury valuation system hardcodes `E8` (10^8) as the divisor when converting a raw ICRC-1 balance to a human-readable token amount, without ever querying `icrc1_decimals`. Because the ICRC-1 standard allows any number of decimals, an SNS token with fewer than 8 decimals causes the treasury to be systematically undervalued, collapsing it into the "small" regime where **no transfer or minting limit applies**. An SNS token with more than 8 decimals causes the opposite: the treasury is overvalued, pushing it into the "large" regime and imposing an artificially tight cap.

---

### Finding Description

In `try_get_balance_valuation_factors`, the raw `Nat` returned by `icrc1_balance_of` is divided by the hardcoded constant `E8 = 100_000_000` (10^8):

```rust
// rs/sns/governance/token_valuation/src/lib.rs, line 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
    ValuationError::new_arithmetic(format!(
        "Balance of {account:?} does not fit in u128: {err:?}"
    ))
})?) / Decimal::from(E8);
``` [1](#0-0) 

The function never calls `icrc1_decimals` on the ledger. The `Icrc1Client` trait used here only exposes `icrc1_balance_of`:

```rust
// rs/sns/governance/token_valuation/src/lib.rs, line 242-246
#[automock]
#[async_trait]
trait Icrc1Client: Send {
    async fn icrc1_balance_of(&mut self, account: Account) -> Result<Nat, (i32, String)>;
}
``` [2](#0-1) 

The resulting `tokens` value feeds directly into `ValuationFactors::to_xdr`, which multiplies `tokens * icps_per_token * xdrs_per_icp` to produce the XDR valuation used by the treasury limit logic: [3](#0-2) 

That XDR valuation is then consumed by `ProposalsAmountTotalUpperBound::in_tokens` to determine which regime applies:

- **Small** (< 100,000 XDR): no limit — any amount can be transferred or minted
- **Medium** (100,000–1,200,000 XDR): 25% of treasury per 7-day window
- **Large** (> 1,200,000 XDR): 300,000 XDR cap per 7-day window [4](#0-3) 

The ICRC-1 standard explicitly supports configurable decimals via `icrc1_decimals` (returns `u8`). The ICRC-1 ledger implementation stores and exposes this as a configurable field: [5](#0-4) 

No code in the SNS governance path validates or enforces that the SNS token ledger must report exactly 8 decimals.

---

### Impact Explanation

**Scenario A — SNS token with fewer than 8 decimals (e.g., 6 decimals):**

- Raw balance from `icrc1_balance_of` for a treasury holding 1,000,000 tokens (6-decimal representation) = `1_000_000_000_000`
- Code divides by 10^8 → computes `10,000` tokens instead of `1,000,000`
- XDR valuation is 100× too low
- A treasury genuinely worth 10,000,000 XDR appears to be worth 100,000 XDR → classified as "small" → **no transfer limit applies**
- Governance proposals can drain the entire treasury in a single 7-day window, bypassing the intended 25%/300,000 XDR caps

**Scenario B — SNS token with more than 8 decimals (e.g., 18 decimals):**

- The treasury appears 10^10× larger than it is
- Classified as "large" → the 300,000 XDR cap applies even when the treasury is tiny
- Legitimate treasury operations are blocked

Scenario A is the higher-severity direction: it allows unlimited `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals to pass the limit check, enabling complete treasury drainage or unbounded token minting within a governance-approved window.

---

### Likelihood Explanation

The ICRC-1 standard does not mandate 8 decimals. Any SNS launched with a token configured to use a non-8-decimal ledger (e.g., 6 decimals to match a wrapped stablecoin convention) will silently trigger this miscalculation. The SNS framework creates ICRC-1 ledgers with configurable decimals and no enforcement of the 8-decimal assumption exists in the valuation path. An unprivileged governance participant who submits a `TransferSnsTreasuryFunds` proposal on such an SNS can exploit the undervaluation to bypass the limit check — no special privileges are required beyond the ability to submit a governance proposal.

---

### Recommendation

1. Extend `Icrc1Client` to include `icrc1_decimals`, and query it alongside `icrc1_balance_of` in `try_get_balance_valuation_factors`.
2. Replace the hardcoded `/ Decimal::from(E8)` with `/ Decimal::from(10_u128.pow(decimals as u32))` using the actual on-chain decimal value.
3. Document the decimal assumption explicitly, and add a validation step that rejects SNS tokens whose ledger reports a decimal count that would cause overflow or underflow in the valuation arithmetic.
4. Apply the same fix to the `fetch_icps_per_sns_token` function, which names its supply variables `*_e8s` but treats them as raw `Nat` values — the variable naming implies an 8-decimal assumption that may not hold.

---

### Proof of Concept

1. Launch an SNS with a token ledger configured to use 6 decimals (valid per ICRC-1 standard).
2. Fund the SNS treasury with 1,000,000 tokens (raw balance = `1_000_000_000_000` in 6-decimal units).
3. Submit a `TransferSnsTreasuryFunds` proposal to transfer the entire treasury.
4. During proposal validation, `try_get_sns_token_balance_valuation` is called:
   - `icrc1_balance_of` returns `Nat(1_000_000_000_000)`
   - Code computes `1_000_000_000_000 / 100_000_000 = 10_000` tokens
   - At a price of 10 XDR/token, valuation = 100,000 XDR → classified as "small"
   - `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` returns `NoLimit`
5. The proposal passes the limit check and the full treasury is transferred, despite the actual value being 100,000,000 XDR (well into the "large" regime where a 300,000 XDR cap should apply). [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L242-246)
```rust
#[automock]
#[async_trait]
trait Icrc1Client: Send {
    async fn icrc1_balance_of(&mut self, account: Account) -> Result<Nat, (i32, String)>;
}
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L36-41)
```rust
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);
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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L855-857)
```rust
    pub fn decimals(&self) -> u8 {
        self.decimals
    }
```
