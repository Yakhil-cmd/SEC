### Title
Hardcoded `E8` Decimal Assumption in SNS Treasury Valuation Produces Incorrect Token Count - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

### Summary

`try_get_balance_valuation_factors` in the SNS governance token valuation library unconditionally divides the raw ICRC-1 balance by the hardcoded constant `E8` (10^8) to convert it to a human-readable token count. The ICRC-1 standard allows any ledger to report a configurable `decimals` value, and the function never queries `icrc1_decimals` from the ledger. If an SNS token ledger has decimals ≠ 8, the computed `tokens` field in `ValuationFactors` is wrong by a factor of `10^(8 - actual_decimals)`, causing the SNS treasury valuation to be inflated or deflated, which directly controls whether `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals are accepted or rejected.

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the function `try_get_balance_valuation_factors` fetches the raw ICRC-1 balance and converts it to a `Decimal` token count by dividing by `E8`:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
    ValuationError::new_arithmetic(format!(
        "Balance of {account:?} does not fit in u128: {err:?}"
    ))
})?) / Decimal::from(E8);   // ← hardcoded 10^8
``` [1](#0-0) 

`E8` is the constant `100_000_000` (8 decimal places), imported from `ic_nervous_system_common`. [2](#0-1) 

The ICRC-1 ledger `InitArgs` exposes `decimals: opt nat8`, meaning any deployed ledger can have a decimal count other than 8. [3](#0-2) 

The ledger stores and uses whatever decimals value is provided at init time: [4](#0-3) 

The SNS init code (`rs/sns/init/src/lib.rs`) builds the SNS token ledger init args without explicitly setting `decimals`, relying on the ledger default: [5](#0-4) 

This means the SNS token ledger's actual decimal count is not queried or validated anywhere in the valuation pipeline. The function `try_get_balance_valuation_factors` is called for both ICP treasury and SNS token treasury: [6](#0-5) 

The resulting `ValuationFactors.tokens` is then used to compute the XDR value of the treasury and to enforce the 7-day treasury transfer upper bound: [7](#0-6) [8](#0-7) 

### Impact Explanation

If an SNS token ledger has `decimals = 6` (e.g., a USDC-like SNS token), the raw balance is divided by `10^8` instead of `10^6`, making the computed `tokens` value **100× smaller** than the actual token count. This causes the treasury valuation to be 100× underestimated, which means the 7-day treasury transfer limit (expressed in XDR) is enforced against a much smaller apparent treasury, potentially blocking all legitimate treasury transfers.

Conversely, if `decimals = 18` (e.g., an ETH-like SNS token), the raw balance is divided by `10^8` instead of `10^18`, making `tokens` **10^10× larger** than the actual count. The treasury appears astronomically large, and the computed upper bound on 7-day transfers becomes enormous, effectively **bypassing the treasury transfer rate limit** entirely. An SNS governance majority could drain the treasury in a single proposal window.

### Likelihood Explanation

The SNS init payload currently does not expose a `decimals` field, so SNS tokens deployed through the standard SNS launch flow default to 8 decimals. However:

1. The ICRC-1 ledger wasm itself accepts any `decimals` value at init time.
2. An SNS root canister (controlled by SNS governance) can upgrade the SNS token ledger with a new wasm that changes the effective decimal representation.
3. The `try_get_balance_valuation_factors` function is architecturally generic — it accepts any `Icrc1Client` — and makes no attempt to query `icrc1_decimals` before dividing.
4. Future SNS init changes or direct ledger upgrades by an SNS governance majority could introduce non-8-decimal SNS tokens, triggering this bug.

The entry path is reachable by any SNS governance participant who can pass a `UpgradeSnsControlledCanister` proposal to upgrade the SNS token ledger.

### Recommendation

Replace the hardcoded `E8` divisor with a dynamic query to `icrc1_decimals` on the target ledger. The `Icrc1Client` trait should be extended with a `icrc1_decimals` method, and `try_get_balance_valuation_factors` should fetch the actual decimals concurrently with the balance and use `10^decimals` as the divisor:

```rust
let decimals = icrc1_client.icrc1_decimals().await?;
let divisor = Decimal::from(10_u128.pow(decimals as u32));
let tokens = Decimal::from(...balance...) / divisor;
```

This mirrors the fix recommended in M-03: make the conversion protocol-specific rather than assuming a fixed precision.

### Proof of Concept

1. Deploy an SNS with a token ledger initialized with `decimals = 6`.
2. Fund the SNS token treasury with `1_000_000` raw units (= 1.0 token at 6 decimals).
3. Call `try_get_sns_token_balance_valuation` on the SNS governance canister.
4. Observe that `valuation_factors.tokens` = `1_000_000 / 10^8` = `0.01` tokens instead of the correct `1.0` token — a 100× underestimate.
5. The resulting XDR valuation is 100× too low, causing the `ProposalsAmountTotalUpperBound` to be computed against a near-zero treasury, blocking all `TransferSnsTreasuryFunds` proposals.

For the over-inflation case, repeat with `decimals = 18`: `1_000_000_000_000_000_000` raw units (= 1.0 token) divided by `10^8` yields `10_000_000_000` tokens — a 10^10× overestimate — causing the rate limit to be effectively disabled. [9](#0-8) [10](#0-9)

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

**File:** rs/nervous_system/humanize/src/lib.rs (L20-20)
```rust
const E8: u64 = 100_000_000;
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L100-104)
```text
type InitArgs = record {
  minting_account : Account;
  fee_collector_account : opt Account;
  transfer_fee : nat;
  decimals : opt nat8;
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L715-715)
```rust
            decimals: decimals.unwrap_or_else(default_decimals),
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
