### Title
Hardcoded `E8` Decimal Divisor in SNS Treasury Valuation Causes Incorrect Transfer/Mint Limits - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

### Summary

`try_get_balance_valuation_factors` in the SNS governance token valuation library unconditionally divides the raw ICRC-1 ledger balance by the hardcoded constant `E8` (10^8) to convert atomic units to whole tokens. The ICRC-1 standard permits any `u8` decimal count (0–255). When an SNS token has decimals ≠ 8, the `tokens` and `icps_per_token` fields in `ValuationFactors` are individually wrong by a factor of `10^(decimals − 8)`, causing the 7-day treasury transfer and minting upper-bound limits to be inflated or deflated by the same factor.

### Finding Description

**Root cause — hardcoded divisor:**

In `try_get_balance_valuation_factors`:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0)...?)
    / Decimal::from(E8);   // ← always 10^8, never queries icrc1_decimals
``` [1](#0-0) 

`E8 = 100_000_000` is a compile-time constant: [2](#0-1) 

The function never calls `icrc1_decimals` on the ledger, even though the ICRC-1 interface exposes it: [3](#0-2) 

**How `icps_per_token` is derived:**

`IcpsPerSnsTokenClient::fetch_icps_per_sns_token` reads `sns_tokens_per_icp` from the swap's `get_derived_state`. The swap computes this as raw atomic SNS units divided by raw ICP e8s:

```rust
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    ...
``` [4](#0-3) 

So `sns_tokens_per_icp` = `SNS_atomic / ICP_e8s`, not `SNS_whole / ICP_whole`. The resulting `icps_per_token` is therefore off by `10^(8 − d_sns)`.

**Error cancellation in XDR valuation but not in limit calculation:**

The XDR valuation `tokens × icps_per_token × xdrs_per_icp` happens to be correct because the two errors cancel:

- `tokens` = `balance_atomic / 10^8` = `balance_whole × 10^(d_sns−8)`
- `icps_per_token` = `ICP_whole × 10^(8−d_sns) / SNS_whole`
- Product = `balance_whole × ICP_whole / SNS_whole` ✓

However, `ProposalsAmountTotalUpperBound::in_tokens` uses `balance_tokens` and `icps_per_token` **individually**, not only their product:

```rust
Self::Fraction(fraction) => balance_tokens.checked_mul(fraction)...
// and
Self::Xdr(max_xdr) => {
    let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token)...;
    let tokens_per_xdr = xdrs_per_token.inv();
    max_xdr.checked_mul(tokens_per_xdr)...
}
``` [5](#0-4) 

For both the `Fraction` and `Xdr` branches the limit is off by `10^(d_sns − 8)`.

**SNS ledger initialization does not enforce 8 decimals:**

The SNS init code builds the ledger with `LedgerInitArgsBuilder::with_symbol_and_name(...)` and never calls `.with_decimals()`: [6](#0-5) 

`InitArgsBuilder::for_tests()` (which `with_symbol_and_name` delegates to) sets `decimals: None`: [7](#0-6) 

When `decimals` is `None`, the ledger falls back to `default_decimals()`: [8](#0-7) 

The ICRC-1 ledger `InitArgs` accepts any `opt nat8` for decimals, and the SNS governance canister performs no on-chain validation of the ledger's decimal count before using it in treasury limit calculations. [9](#0-8) 

### Impact Explanation

For an SNS token with `d_sns > 8` (e.g., 18 decimals as used by ckETH):

- `balance_tokens` = `balance_whole × 10^10` (10 billion times too large)
- `Fraction` limit = `balance_whole × 10^10 × 0.25` instead of `balance_whole × 0.25`
- `Xdr` limit = `300,000 XDR × tokens_per_xdr × 10^10` instead of `300,000 XDR × tokens_per_xdr`

The 7-day treasury transfer cap (`transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`) and minting cap (`mint_sns_tokens_7_day_total_upper_bound_tokens`) are both rendered effectively unlimited, allowing SNS governance proposals to drain the entire treasury or mint unbounded tokens in a single 7-day window — far beyond the intended 25%-of-treasury or 300,000-XDR caps. [10](#0-9) 

### Likelihood Explanation

The standard SNS deployment path does not call `.with_decimals()`, so the actual decimal count depends on `default_decimals()`. If that function returns 8 (the most likely value given the ICP ecosystem convention), currently deployed SNS tokens are unaffected. However:

1. The ICRC-1 standard explicitly allows any `u8` decimal value.
2. Nothing in the SNS governance canister validates or enforces 8 decimals at runtime.
3. A future SNS deployment or ledger upgrade path that introduces non-8-decimal tokens would silently break the treasury limits.
4. The `IcpsPerSnsTokenClient` variable names (`initial_supply_e8s`, `current_supply_e8s`) reveal the implicit assumption is never checked. [11](#0-10) 

Likelihood is **medium** given the current ecosystem convention of 8 decimals, but the code is structurally broken for any non-8-decimal ICRC-1 token used as an SNS token.

### Recommendation

1. Query `icrc1_decimals` from the ledger canister at valuation time (or cache it at SNS init) and use the actual decimal count as the divisor instead of the hardcoded `E8`.
2. Add an on-chain assertion in SNS governance initialization that the SNS ledger's `icrc1_decimals` equals 8, or generalize all e8s-denominated arithmetic to use the actual decimal count.
3. Audit `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` — the `sns_tokens_per_icp` field from the swap is also in atomic units and must be normalized by `10^(d_sns − 8)` to yield a correct whole-token price.

### Proof of Concept

**Setup:** Deploy an SNS whose ICRC-1 ledger is initialized with `decimals = 18` (as ckETH uses). Treasury holds 100 whole SNS tokens = `100 × 10^18` atomic units.

**Observed computation:**

```
balance_atomic = 100 × 10^18
tokens         = 100 × 10^18 / 10^8  = 100 × 10^10   (should be 100)

swap raised 1000 ICP for 1,000,000 SNS tokens:
  sns_tokens_per_icp = (10^6 × 10^18) / (10^3 × 10^8) = 10^13
  icps_per_token     = 1 / 10^13                        (should be 0.001)

XDR valuation = 100×10^10 × 10^-13 × xdrs_per_icp
              = 0.1 × xdrs_per_icp  ✓  (correct, errors cancel)

Fraction limit = balance_tokens × 0.25
              = 100×10^10 × 0.25 = 25×10^10 tokens
                (should be 25 tokens — off by 10^10)
```

A governance proposal to transfer `25 × 10^10` whole SNS tokens passes the limit check, draining the entire treasury (and far beyond) in a single proposal window. [1](#0-0) [12](#0-11)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L177-181)
```rust
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L316-330)
```rust
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
```

**File:** rs/nervous_system/humanize/src/lib.rs (L20-20)
```rust
const E8: u64 = 100_000_000;
```

**File:** packages/icrc-ledger-client/src/lib.rs (L45-50)
```rust
    pub async fn decimals(&self) -> Result<u8, (i32, String)> {
        self.runtime
            .call(self.ledger_canister_id, "icrc1_decimals", ())
            .await
            .map(untuple)
    }
```

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L8-18)
```rust
pub fn transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}

pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L76-110)
```rust
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
```

**File:** rs/sns/init/src/lib.rs (L597-618)
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
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L161-188)
```rust
    pub fn for_tests() -> Self {
        let default_owner = Principal::anonymous();
        Self(InitArgs {
            minting_account: Account {
                owner: default_owner,
                subaccount: None,
            },
            fee_collector_account: None,
            initial_balances: vec![],
            transfer_fee: 10_000_u32.into(),
            decimals: None,
            token_name: "Test Token".to_string(),
            token_symbol: "XTK".to_string(),
            metadata: vec![],
            archive_options: ArchiveOptions {
                trigger_threshold: 1000,
                num_blocks_to_archive: 1000,
                node_max_memory_size_bytes: None,
                max_message_size_bytes: None,
                controller_id: default_owner.into(),
                more_controller_ids: None,
                cycles_for_archive_creation: Some(0),
                max_transactions_per_response: None,
            },
            max_memo_length: None,
            feature_flags: None,
            index_principal: None,
        })
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L715-715)
```rust
            decimals: decimals.unwrap_or_else(default_decimals),
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L100-104)
```text
type InitArgs = record {
  minting_account : Account;
  fee_collector_account : opt Account;
  transfer_fee : nat;
  decimals : opt nat8;
```
