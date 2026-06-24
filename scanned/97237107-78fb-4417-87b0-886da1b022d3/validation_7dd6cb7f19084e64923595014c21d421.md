### Title
SNS Treasury Valuation Hardcodes 8-Decimal Assumption, Corrupting Transfer/Mint Limits - (`rs/sns/governance/token_valuation/src/lib.rs`)

### Summary

The SNS governance treasury valuation function `try_get_balance_valuation_factors` unconditionally divides the raw ICRC-1 balance by the hardcoded constant `E8` (= 10^8) to convert it to "whole tokens." This silently assumes every token handled by the SNS governance system has exactly 8 decimal places. If an SNS ledger is initialized or upgraded with a different decimal count, the computed treasury valuation will be wrong by orders of magnitude, causing the 7-day spending limits on `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals to be either far too permissive or far too restrictive.

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the private function `try_get_balance_valuation_factors` fetches the raw ICRC-1 balance and converts it to a `Decimal` representing whole tokens:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0)...) / Decimal::from(E8);
``` [1](#0-0) 

`E8` is the constant `100_000_000` (10^8), imported from `ic_nervous_system_common`. [2](#0-1) 

This same function is called for both ICP treasury valuations (`try_get_icp_balance_valuation`) and SNS token treasury valuations (`try_get_sns_token_balance_valuation`): [3](#0-2) 

The resulting `ValuationFactors.tokens` (in whole-token units) is then multiplied by `icps_per_token` and `xdrs_per_icp` to produce the XDR valuation used to enforce the 7-day treasury spending cap: [4](#0-3) 

This valuation directly gates `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals: [5](#0-4) 

The `proposal_amount_tokens` conversion for both proposal types also hardcodes `E8`: [6](#0-5) [7](#0-6) 

The ICRC-1 ledger standard supports any `u8` decimal count (0–255). The SNS ledger initialization code (`rs/sns/init/src/lib.rs`) does not explicitly set `decimals`, relying on the ICRC-1 ledger's default of 8: [8](#0-7) 

However, the `ManageLedgerParameters` SNS proposal type does not include a `decimals` field, so decimals cannot be changed post-deployment through the standard governance path: [9](#0-8) 

The `AdvanceSnsTargetVersion` proposal can upgrade the SNS ledger WASM. If a future ledger version changes the default decimal count, or if an SNS is deployed with a custom ledger having non-8 decimals, the hardcoded `E8` divisor will produce a wrong valuation.

Additionally, `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` computes inflation as `current_supply_raw / initial_supply_raw` (both in raw units, so the ratio is decimal-agnostic), but the `sns_tokens_per_icp` field from the swap canister is expressed in **whole tokens per ICP**. If the ledger has non-8 decimals, the `try_get_balance_valuation_factors` division by `E8` will produce a wrong whole-token count, making the final XDR valuation incorrect. [10](#0-9) 

### Impact Explanation

- **If SNS token decimals < 8** (e.g., 6): `balance_raw / 10^8` underestimates the true whole-token count by `10^(8-6) = 100×`. The treasury appears 100× smaller than it is. The 7-day spending cap is 100× too permissive, allowing `TransferSnsTreasuryFunds` or `MintSnsTokens` proposals to drain far more than the intended limit.
- **If SNS token decimals > 8** (e.g., 18): `balance_raw / 10^8` overestimates the true whole-token count by `10^(18-8) = 10^10×`. The treasury appears 10 billion times larger. The 7-day spending cap becomes impossibly large, effectively removing the limit entirely, or alternatively the `tokens_to_e8s` conversion overflows and blocks legitimate proposals.

The `treasury_valuation_amount_e8s` function in SNS governance also re-uses this valuation to report the treasury balance in e8s, compounding the error: [11](#0-10) 

### Likelihood Explanation

**Low in the current codebase.** All deployed SNS tokens use the ICRC-1 ledger's default of 8 decimals, and the standard SNS init flow does not expose a `decimals` parameter. However:

1. The `AdvanceSnsTargetVersion` proposal can upgrade the SNS ledger WASM. A future ledger version with a different default decimal count would silently corrupt all treasury valuations.
2. An SNS community could vote to deploy a custom ledger with non-8 decimals.
3. The code is architecturally generic (it accepts any `sns_ledger_canister_id`) but does not query `icrc1_decimals` before performing the division, making it fragile by design.

The entry path is: any SNS neuron holder submitting a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal after the ledger decimal count diverges from 8.

### Recommendation

Replace the hardcoded `E8` divisor with a dynamic query to `icrc1_decimals` on the relevant ledger canister. In `try_get_balance_valuation_factors`, fetch the decimals alongside the balance and use `10^decimals` as the divisor:

```rust
let decimals = icrc1_client.icrc1_decimals().await?;
let divisor = Decimal::from(10u128.pow(decimals as u32));
let tokens = Decimal::from(...balance...) / divisor;
```

Similarly, `proposal_amount_tokens` and `tokens_to_e8s` should use the actual ledger decimals rather than the hardcoded `E8`.

### Proof of Concept

Suppose an SNS ledger is upgraded to a WASM where `icrc1_decimals()` returns `6` (like USDC). The SNS treasury holds `1_000_000` raw units = 1 whole token.

- **Correct valuation**: `1_000_000 / 10^6 = 1.0` token
- **Actual computation**: `1_000_000 / 10^8 = 0.01` token

The treasury is valued at 1% of its true size. If the 7-day limit is 10% of treasury value, the governance canister will allow proposals totaling only `0.001` tokens instead of `0.1` tokens — blocking all legitimate treasury transfers. Conversely, with 18-decimal tokens, the treasury appears `10^10×` larger, removing the spending cap entirely and allowing unlimited `TransferSnsTreasuryFunds` proposals to pass validation.

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L6-6)
```rust
use ic_nervous_system_common::{E8, UNITS_PER_PERMYRIAD, i2d};
```

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-415)
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
    }
```

**File:** rs/sns/governance/src/proposal.rs (L799-814)
```rust
    // Finally, inspect the proposal's amount: it must not exceed max - spent (remainder). Or if
    // you prefer, equivalently, amount + spent must be <= max.
    let allowance_remainder_tokens = max_tokens.checked_sub(spent_tokens).ok_or_else(|| {
        format!("Arithmetic error while performing {max_tokens} - {spent_tokens}",)
    })?;
    let proposal_amount_tokens = action.proposal_amount_tokens()?;
    if proposal_amount_tokens > allowance_remainder_tokens {
        // Although it might not be obvious to the user, their proposal is invalid, and we
        // consider it to be "their fault".
        return Err(format!(
            "Amount is too large. Within the past 7 days, a total of {spent_tokens} tokens has already \
             been executed in like proposals. Whereas, at most {max_tokens} is allowed. An additional \
             {proposal_amount_tokens} tokens from this proposal would cause that upper bound to be exceeded. \
             Maybe, try again in a few days?"
        ));
    }
```

**File:** rs/sns/governance/src/proposal.rs (L839-849)
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
    }
```

**File:** rs/sns/governance/src/proposal.rs (L1003-1015)
```rust
    fn proposal_amount_tokens(&self) -> Result<Decimal, String> {
        let amount_e8s = self
            .amount_e8s
            // This Err only occurs when self is invalid.
            .ok_or_else(|| "The `amount_e8s` field is not populated.".to_string())?;

        denominations_to_tokens(amount_e8s, E8)
            // This Err will not be generated, because we are dividing a u64 (amount_e8s) by a
            // positive number (E8).
            .ok_or_else(
                || format!("Unable to convert proposal amount {amount_e8s} e8s to tokens.",),
            )
    }
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

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L455-463)
```rust
/// A proposal function that changes the ledger's parameters.
/// Fields with None values will remain unchanged.
#[derive(Default, candid::CandidType, candid::Deserialize, Debug, Clone, PartialEq)]
pub struct ManageLedgerParameters {
    pub transfer_fee: Option<u64>,
    pub token_name: Option<String>,
    pub token_symbol: Option<String>,
    pub token_logo: Option<String>,
}
```

**File:** rs/sns/governance/src/governance.rs (L5101-5122)
```rust
    async fn treasury_valuation_amount_e8s(&self, treasury: i32) -> Result<u64, String> {
        let token = interpret_token_code(treasury)
            .map_err(|err| format!("Failed to interpret treasury token code {treasury}: {err}"))?;

        let treasury_valuation_result = assess_treasury_balance(
            token,
            self.env.canister_id(),
            self.ledger.canister_id(),
            self.proto.swap_canister_id_or_panic(),
        )
        .await;

        let treasury_valuation = treasury_valuation_result
            .map_err(|err| format!("Failed to assess treasury balance for {token:?}: {err}"))?;

        let amount_e8s =
            tokens_to_e8s(treasury_valuation.valuation_factors.tokens).map_err(|err| {
                format!("Failed to convert treasury balance to e8s for {token:?}: {err}")
            })?;

        Ok(amount_e8s)
    }
```
