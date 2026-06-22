### Title
Hardcoded 8-Decimal Assumption in SNS Treasury Valuation Bypasses Transfer/Mint Limits - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
`try_get_balance_valuation_factors` in the SNS governance token valuation library divides the raw ICRC-1 ledger balance by the hardcoded constant `E8` (10^8) to convert atomic units to tokens, without ever querying `icrc1_decimals` on the SNS ledger. Because the ICRC-1 standard allows any decimal count, an SNS whose native token uses a non-8-decimal configuration will produce a wildly incorrect treasury valuation, which is then used to enforce the 7-day `TransferSnsTreasuryFunds` and `MintSnsTokens` proposal limits.

### Finding Description
In `rs/sns/governance/token_valuation/src/lib.rs`, the function `try_get_balance_valuation_factors` fetches the raw balance from the SNS ledger via `icrc1_balance_of` and converts it to a human-scale token amount by dividing by the hardcoded constant `E8 = 100_000_000`:

```rust
// line 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
    ValuationError::new_arithmetic(format!(
        "Balance of {account:?} does not fit in u128: {err:?}"
    ))
})?) / Decimal::from(E8);
``` [1](#0-0) 

The function never calls `icrc1_decimals` on the ledger canister. The `Icrc1Client` trait used here only exposes `icrc1_balance_of`:

```rust
trait Icrc1Client: Send {
    async fn icrc1_balance_of(&mut self, account: Account) -> Result<Nat, (i32, String)>;
}
``` [2](#0-1) 

The ICRC-1 ledger standard exposes `icrc1_decimals` as a configurable `u8` field, and the SNS ICRC-1 ledger stores it as a runtime parameter set at init time:

```rust
decimals: decimals.unwrap_or_else(default_decimals),
``` [3](#0-2) 

The resulting `tokens` value feeds directly into `ValuationFactors::to_xdr()`:

```rust
tokens * icps_per_token * xdrs_per_icp
``` [4](#0-3) 

This XDR valuation is what SNS governance uses to compute the 7-day upper bound for `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals via `assess_treasury_balance` → `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` / `mint_sns_tokens_7_day_total_upper_bound_tokens`. [5](#0-4) 

### Impact Explanation
- **SNS token with >8 decimals (e.g., 18):** The raw balance is in units of 10^-18 per token. Dividing by 10^8 instead of 10^18 inflates the computed `tokens` by a factor of 10^10. The treasury XDR valuation is 10^10× too large, making the 7-day transfer/mint limit 10^10× higher than intended. This effectively nullifies the treasury protection: a governance proposal can drain or mint the entire treasury in a single 7-day window without triggering the limit.
- **SNS token with <8 decimals (e.g., 6):** The valuation is 100× too small, making the limit 100× lower than intended. Legitimate treasury proposals are blocked even when the actual transfer is well within the intended safety threshold.

The most dangerous case is the >8-decimal scenario, which allows a governance majority (which may be a small quorum in a newly launched SNS) to bypass the treasury protection limits entirely.

### Likelihood Explanation
The ICRC-1 standard explicitly supports configurable decimals, and the SNS ledger init args accept an optional `decimals` field. Any SNS launched with a token whose decimals differ from 8 — whether intentionally or by developer error — is affected. The entry path is a standard governance proposal (`TransferSnsTreasuryFunds` or `MintSnsTokens`) submitted by any neuron holder with sufficient voting power, which is a normal, unprivileged operation. No special access is required beyond holding SNS neurons.

### Recommendation
Replace the hardcoded `E8` divisor with a runtime query to `icrc1_decimals` on the ledger canister. Extend the `Icrc1Client` trait to include a `decimals()` method, fetch it alongside the balance, and compute the divisor as `10_u128.pow(decimals as u32)`. For the ICP case, the existing hardcoded value of 8 remains correct and can be kept as a constant.

### Proof of Concept
1. Deploy an SNS whose native token ledger is initialized with `decimals = 18`.
2. Mint 1 SNS token to the treasury (raw balance = 10^18 atomic units).
3. SNS governance calls `try_get_sns_token_balance_valuation` when a `TransferSnsTreasuryFunds` proposal is submitted.
4. `try_get_balance_valuation_factors` computes `tokens = 10^18 / 10^8 = 10^10` instead of `1`.
5. The XDR valuation of the treasury is computed as `10^10 × icps_per_token × xdrs_per_icp`, which is 10 billion times the true value.
6. `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` returns a limit 10^10× higher than intended.
7. A proposal to transfer the entire treasury (1 real token) passes the limit check and executes, even though the true treasury value is tiny.

The root cause is exclusively in IC production code at: [1](#0-0) 
with no dependency on any external oracle behavior — the SNS ledger's own `icrc1_balance_of` return value is misinterpreted due to the hardcoded decimal assumption.

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L177-181)
```rust
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L242-246)
```rust
#[automock]
#[async_trait]
trait Icrc1Client: Send {
    async fn icrc1_balance_of(&mut self, account: Account) -> Result<Nat, (i32, String)>;
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L715-715)
```rust
            decimals: decimals.unwrap_or_else(default_decimals),
```

**File:** rs/sns/governance/src/treasury.rs (L256-270)
```rust
pub(crate) async fn assess_treasury_balance(
    token: Token,
    sns_governance_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, String> {
    let treasury_account = token.treasury_account(sns_governance_canister_id)?;
    let valuation = token
        .assess_balance(sns_ledger_canister_id, swap_canister_id, treasury_account)
        .await
        .map_err(|valuation_error| {
            format!("Unable to assess current treasury balance: {valuation_error:?}")
        })?;
    Ok(valuation)
}
```
