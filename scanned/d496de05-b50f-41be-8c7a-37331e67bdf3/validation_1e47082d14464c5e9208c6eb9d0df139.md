### Title
Hardcoded `E8` Divisor in SNS Treasury Valuation Ignores Actual Token Decimals - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary

`try_get_balance_valuation_factors` in the SNS governance token valuation library unconditionally divides the raw ICRC-1 balance by the hardcoded constant `E8` (10^8) to convert to whole tokens. This assumes every token has exactly 8 decimal places. ICRC-1 tokens may have any decimal count (0–255). For a token with fewer than 8 decimals the treasury is undervalued, causing the 7-day transfer-limit guard to be too permissive; for a token with more than 8 decimals the treasury is overvalued, making the guard too restrictive.

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the function `try_get_balance_valuation_factors` converts the raw `icrc1_balance_of` response to a "whole-token" count by dividing by the constant `E8`:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0)…?)
    / Decimal::from(E8);   // ← hardcoded 10^8
``` [1](#0-0) 

`E8` is defined as `100_000_000` (10^8): [2](#0-1) 

The ICRC-1 ledger `InitArgs` accepts an optional `decimals : opt nat8` field, meaning any value 0–255 is valid: [3](#0-2) 

The ledger stores whatever decimal value is supplied at initialization: [4](#0-3) 

The resulting `ValuationFactors.tokens` field is then multiplied by `icps_per_token` and `xdrs_per_icp` to produce the XDR value used to enforce the 7-day treasury-transfer cap: [5](#0-4) 

The cap logic classifies the treasury as small/medium/large and sets the allowed transfer ceiling accordingly: [6](#0-5) 

### Impact Explanation

If an SNS token has **fewer than 8 decimals** (e.g., 6), the raw balance is divided by 10^8 instead of 10^6, undervaluing the treasury by 10^(8−6) = 100×. A treasury that should be classified as "medium" (e.g., 500 000 XDR) is classified as "small" (5 000 XDR), triggering the `NoLimit` branch and allowing unlimited 7-day treasury transfers — bypassing the intended governance safety rail.

If an SNS token has **more than 8 decimals** (e.g., 18), the treasury is overvalued by 10^10, pushing it into the "large" regime and capping transfers at 300 000 XDR even when the treasury is tiny.

The same `E8` assumption is also embedded in `proposal_amount_tokens` and `total_treasury_transfer_amount_tokens`: [7](#0-6) 

### Likelihood Explanation

**Currently low.** The standard SNS initialization path (`rs/sns/init/src/lib.rs`) builds ledger init args without calling `.with_decimals(…)`, so the ledger defaults to 8 decimals: [8](#0-7) 

Because the `SnsInitPayload` proto does not expose a `token_decimals` field, all SNS tokens deployed through the NNS SNS-W canister today have exactly 8 decimals, and the bug is latent. However, the valuation library is written as a general ICRC-1 client with no enforcement of the 8-decimal assumption, so any future extension of the SNS init payload to expose custom decimals, or any SNS deployed with a custom ledger, would immediately trigger the miscalculation.

### Recommendation

Replace the hardcoded `E8` divisor with a live query to `icrc1_decimals` on the same ledger canister, then compute `10_u128.pow(decimals as u32)` as the divisor. The `Icrc1Client` trait should be extended with a `icrc1_decimals` method, and `try_get_balance_valuation_factors` should fetch and use the actual decimal count:

```rust
let decimals = icrc1_client.icrc1_decimals().await?;
let divisor = Decimal::from(10_u128.pow(decimals as u32));
let tokens = Decimal::from(raw_balance) / divisor;
```

The same fix must be applied to `proposal_amount_tokens` and `total_treasury_transfer_amount_tokens`, which also hardcode `E8` when converting proposal amounts to whole-token counts.

### Proof of Concept

1. Deploy an SNS whose ICRC-1 ledger is initialized with `decimals = 6`.
2. Fund the SNS treasury with 500 000 000 000 smallest-units (= 500 000 whole tokens at 6 decimals, worth e.g. 500 000 XDR).
3. Call `try_get_balance_valuation_factors`: the code computes `500_000_000_000 / 1e8 = 5 000` whole tokens → XDR value = 5 000 XDR → classified as "small" → `NoLimit`.
4. Submit a `TransferSnsTreasuryFunds` proposal for the entire treasury. The proposal passes the 7-day cap check with no limit, draining the treasury in a single proposal — far beyond the intended 300 000 XDR ceiling. [9](#0-8)

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

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L135-137)
```rust
pub const DECIMAL_PLACES: u32 = 8;
/// How many times can Tokens be divided
pub const TOKEN_SUBDIVIDABLE_BY: u64 = 100_000_000;
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L100-122)
```text
type InitArgs = record {
  minting_account : Account;
  fee_collector_account : opt Account;
  transfer_fee : nat;
  decimals : opt nat8;
  max_memo_length : opt nat16;
  token_symbol : text;
  token_name : text;
  metadata : vec record { text; MetadataValue };
  initial_balances : vec record { Account; nat };
  feature_flags : opt FeatureFlags;
  archive_options : record {
    num_blocks_to_archive : nat64;
    max_transactions_per_response : opt nat64;
    trigger_threshold : nat64;
    max_message_size_bytes : opt nat64;
    cycles_for_archive_creation : opt nat64;
    node_max_memory_size_bytes : opt nat64;
    controller_id : principal;
    more_controller_ids : opt vec principal
  };
  index_principal : opt principal
};
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L715-715)
```rust
            decimals: decimals.unwrap_or_else(default_decimals),
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-115)
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
    }

```

**File:** rs/sns/governance/src/proposal.rs (L839-848)
```rust
    fn proposal_amount_tokens(&self) -> Result<Decimal, String> {
        denominations_to_tokens(self.amount_e8s, E8)
            // This Err will not be generated, because we are dividing a u64 (amount_e8s) by a
            // positive number (E8).
            .ok_or_else(|| {
                format!(
                    "Unable to convert proposal amount {} e8s to tokens.",
                    self.amount_e8s,
                )
            })
```

**File:** rs/sns/init/src/lib.rs (L597-630)
```rust
        let mut payload_builder =
            LedgerInitArgsBuilder::with_symbol_and_name(token_symbol, token_name)
                .with_minting_account(sns_canister_ids.governance.0)
                .with_transfer_fee(
                    self.transaction_fee_e8s
                        .unwrap_or(DEFAULT_TRANSFER_FEE.get_e8s()),
                )
                .with_archive_options(ArchiveOptions {
                    trigger_threshold: 2000,
                    num_blocks_to_archive: 1000,
                    // 1 GB, which gives us 3 GB space when upgrading
                    node_max_memory_size_bytes: Some(1024 * 1024 * 1024),
                    // 128kb
                    max_message_size_bytes: Some(128 * 1024),
                    controller_id: root_canister_id.get(),
                    more_controller_ids: None,
                    // TODO: allow users to set this value
                    // 10 Trillion cycles
                    cycles_for_archive_creation: Some(10_000_000_000_000),
                    max_transactions_per_response: None,
                })
                .with_index_principal(Principal::from(sns_canister_ids.index));

        if let Some(token_logo) = &self.token_logo {
            payload_builder = payload_builder.with_metadata_entry(
                ICRC1_TOKEN_LOGO_KEY,
                MetadataValue::Text(token_logo.clone()),
            );
        }

        for (account, amount) in self.get_all_ledger_accounts(sns_canister_ids)? {
            payload_builder = payload_builder.with_initial_balance(account, amount);
        }
        Ok(LedgerArgument::Init(payload_builder.build()))
```
