### Title
Hardcoded `E8` Divisor in Token Valuation Assumes 8 Decimals for All Tokens, Producing Incorrect Treasury Valuations - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
In `try_get_balance_valuation_factors`, the raw ICRC-1 balance is always divided by the hardcoded constant `E8` (`1e8`) to convert it to whole tokens. This silently assumes every token has exactly 8 decimal places. SNS tokens created via the ICRC-1 standard can have a different number of decimals. When they do, the computed `tokens` field in `ValuationFactors` is wrong by a factor of `10^(8 - actual_decimals)`, causing the SNS treasury valuation—and therefore the governance-enforced transfer/mint limits—to be proportionally wrong.

### Finding Description

`try_get_balance_valuation_factors` is the single shared path used to value both ICP balances and SNS-token balances:

```rust
// rs/sns/governance/token_valuation/src/lib.rs  line 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
    ValuationError::new_arithmetic(format!(
        "Balance of {account:?} does not fit in u128: {err:?}"
    ))
})?) / Decimal::from(E8);   // <-- hardcoded 1e8
``` [1](#0-0) 

`E8` is defined as `100_000_000` (8 decimal places): [2](#0-1) 

The function is called for SNS tokens via `try_get_sns_token_balance_valuation`, which passes the SNS ledger canister directly without querying its actual decimal count: [3](#0-2) 

The resulting `ValuationFactors.tokens` is then multiplied by `icps_per_token` and `xdrs_per_icp` to produce the XDR valuation used by `ProposalsAmountTotalUpperBound`: [4](#0-3) 

`icps_per_token` is derived from the swap canister's `sns_tokens_per_icp` field, which is expressed in **whole tokens per ICP** (not e8s), as confirmed by the test fixture: [5](#0-4) 

This means the only place where the decimal count matters is the `/ Decimal::from(E8)` division, and it is hardcoded.

The XDR valuation drives the treasury-transfer and token-minting upper bounds enforced by SNS governance: [6](#0-5) 

### Impact Explanation

| SNS token decimals | Error factor | Effect on treasury limit |
|---|---|---|
| 6 (e.g. USDC-style) | 100× undervaluation | Limit is 100× too restrictive; legitimate proposals blocked |
| 18 (e.g. ERC-20-style) | 10,000,000,000× overvaluation | Limit is 10^10× too permissive; SNS can drain its entire treasury in a single proposal |

The 18-decimal case is the dangerous direction: an SNS whose token ledger reports balances in 1e18 units would have its treasury valued at 10^10 times its true XDR worth, making the `ProposalsAmountTotalUpperBound` effectively unlimited. A governance majority of that SNS could then pass a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal that moves the entire treasury in one shot, bypassing the intended rate-limiting safeguard.

### Likelihood Explanation

The ICRC-1 standard explicitly supports variable decimals. The SNS initialization flow accepts token parameters including `decimals` from the SNS creator. An SNS creator (a canister developer submitting an SNS proposal to the NNS) can set `decimals` to any value. No on-chain enforcement in the valuation path queries `icrc1_decimals()` before dividing. The entry path is an unprivileged canister developer submitting a valid SNS initialization proposal with non-8 decimals, followed by a governance majority passing a treasury proposal.

### Recommendation

Replace the hardcoded `E8` divisor with a runtime query to `icrc1_decimals()` on the ledger canister, then compute `10_u128.pow(decimals)` as the divisor:

```rust
let decimals: u8 = /* call icrc1_client.icrc1_decimals().await? */;
let divisor = Decimal::from(10_u128.pow(decimals as u32));
let tokens = Decimal::from(raw_balance) / divisor;
```

This mirrors the fix recommended in M-02: always derive the scaling factor from the token's actual decimal count rather than assuming a fixed value.

### Proof of Concept

Assume an SNS token with 18 decimals and a treasury balance of 1,000 whole tokens:

- Raw balance returned by `icrc1_balance_of`: `1_000 * 10^18 = 1e21`
- Current code: `tokens = 1e21 / 1e8 = 1e13` (13 trillion "tokens")
- Correct: `tokens = 1e21 / 1e18 = 1_000` (one thousand tokens)

If 1,000 tokens are worth 10,000 XDR, the current code computes a treasury value of `1e13 * (price_per_token_in_xdr)`, which is `1e10×` the true value. `ProposalsAmountTotalUpperBound` would classify this as a "large" treasury and apply the `MAX_XDR = 300,000` cap—but that cap is then converted back to tokens using the same inflated valuation, yielding an allowed transfer of `300_000 / (inflated_xdrs_per_token) ≈ entire real treasury`, defeating the rate-limiting mechanism entirely. [1](#0-0) [7](#0-6)

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L177-181)
```rust
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
```

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L135-137)
```rust
pub const DECIMAL_PLACES: u32 = 8;
/// How many times can Tokens be divided
pub const TOKEN_SUBDIVIDABLE_BY: u64 = 100_000_000;
```

**File:** rs/sns/governance/token_valuation/src/tests.rs (L107-128)
```rust
    const INITIAL_ICPS_PER_SNS_TOKEN: f64 = 12.34;
    const INITIAL_SNS_TOKEN_SUPPLY_E8S: u64 = 123_456_789;
    const GENESIS_TIMESTAMP_NANOSECONDS: u64 = 42;

    thread_local! {
        // HashMap is used, because calls are made concurrently; therefore, our
        // usual way of using Vec probably would not work (and wouldn't be
        // right), because we do not want to impose such ordering constraints
        // (on the code under test).
        #[allow(clippy::type_complexity)]
        static EXPECTED_CALLS: RefCell<HashMap<
            (CanisterId, String), // (canister_id, method_name)
            (Vec<u8>, Vec<u8>)     // (request, response)
        >> = {
            RefCell::new(hashmap! {
                // This is used to determine the SNS token price at genesis, as
                // determined by the SNS's initialization swap.
                (*SWAP_CANISTER_ID, "get_derived_state".to_string()) => {
                    let request = encode_args((GetDerivedStateRequest {},)).unwrap();

                    let response = encode_args((GetDerivedStateResponse {
                        sns_tokens_per_icp: Some(1.0 / INITIAL_ICPS_PER_SNS_TOKEN),
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L36-41)
```rust
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-113)
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
```
