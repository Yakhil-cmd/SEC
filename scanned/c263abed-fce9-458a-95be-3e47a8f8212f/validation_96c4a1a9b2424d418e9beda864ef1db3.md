### Title
Hardcoded `E8` Divisor in SNS Treasury Valuation Assumes 8 Decimals for All SNS Tokens — (File: rs/sns/governance/token_valuation/src/lib.rs)

---

### Summary

The function `try_get_balance_valuation_factors` in the SNS governance token valuation module hardcodes the constant `E8` (`100_000_000`, i.e., 10^8) as the divisor when converting a raw ICRC-1 balance to a "tokens" `Decimal`. This silently assumes every token — including SNS tokens — has exactly 8 decimal places. Because the ICRC-1 standard permits any number of decimals and the SNS framework does not enforce 8 decimals, any SNS whose ledger is initialised with a different decimal count will have its treasury valuation computed incorrectly. The wrong valuation is then fed directly into the 7-day spending-limit guard for `TransferSnsTreasuryFunds` proposals, causing the regime classification (small / medium / large treasury) to be wrong and the enforced cap to be either too permissive or too restrictive.

---

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the private function `try_get_balance_valuation_factors` is the single code path used for both ICP treasury valuation (`try_get_icp_balance_valuation`) and SNS token treasury valuation (`try_get_sns_token_balance_valuation`).

```rust
// rs/sns/governance/token_valuation/src/lib.rs  lines 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0)…?)
    / Decimal::from(E8);   // ← hardcoded 10^8 for every token type
```

`E8` is the constant `100_000_000` defined in `rs/nervous_system/common/src/lib.rs` line 61. It is correct for ICP (8 decimals) but is wrong for any SNS token whose ledger was initialised with a different `decimals` value.

The resulting `tokens` field of `ValuationFactors` is used in two critical places:

1. **`ValuationFactors::to_xdr()`** (`lib.rs` line 118-126) — multiplies `tokens × icps_per_token × xdrs_per_icp` to produce the XDR value of the treasury. A wrong `tokens` value produces a wrong XDR value.

2. **`transfer_sns_treasury_funds_7_day_total_upper_bound_tokens()`** (called from `rs/sns/governance/src/proposal.rs` lines 2606, 863) — classifies the treasury as *small* (< 100 k XDR → 100 % limit), *medium* (100 k–1.2 M XDR → 25 % limit), or *large* (> 1.2 M XDR → 300 k XDR cap) and returns the maximum tokens that may be transferred in 7 days.

The proposal-amount side of the same check also divides by `E8`:

```rust
// rs/sns/governance/src/proposal.rs  line 2632
let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)…;
```

Because both sides of the comparison use `E8`, the raw ratio is preserved — but the **XDR thresholds are fixed absolute values**. A wrong XDR valuation therefore places the treasury in the wrong regime, making the enforced cap incorrect.

**Concrete scenario (token with 6 decimals, e.g. a USDC-like SNS token):**

| | Correct (6 dec) | Code (hardcodes 8 dec) |
|---|---|---|
| Treasury balance | 10^12 smallest units = 1 000 000 tokens | 10^12 / 10^8 = **10 000 "tokens"** |
| Token price | 0.5 ICP/token, 1 ICP = 1 XDR | same |
| XDR value | 500 000 XDR → **medium** (25 % limit) | 5 000 XDR → **small** (100 % limit) |
| 7-day limit | 250 000 tokens | 10 000 code-"tokens" ≡ **1 000 000 real tokens** |

A governance majority can therefore submit a `TransferSnsTreasuryFunds` proposal for 300 000 tokens (= 3 × 10^11 smallest units). The code computes `transfer_amount_tokens = 3 × 10^11 / 10^8 = 3 000`, which is below the inflated limit of 10 000, so the check passes. The correct limit of 250 000 tokens would have blocked this transfer.

The root cause is structurally identical to the external report: the wrong token's decimal count is used in the conversion formula.

---

### Impact Explanation

**Governance conservation bug.** The 7-day treasury-transfer spending limit — the primary on-chain safety mechanism preventing rapid treasury drainage — is miscalculated for any SNS whose token has a decimal count other than 8. For tokens with fewer than 8 decimals the limit is too permissive (a governance majority can transfer more than the intended fraction of the treasury in a single 7-day window). For tokens with more than 8 decimals the limit is too restrictive (legitimate proposals may be blocked). The most dangerous direction is the permissive one: a coordinated governance majority could drain a disproportionate share of the treasury in a single proposal cycle, bypassing the safety cap that the protocol is supposed to enforce.

---

### Likelihood Explanation

**Low–Medium.** The ICRC-1 standard and the SNS ledger `InitArgs` both allow any `decimals` value; the SNS framework does not enforce 8 decimals. The default is 8, so most deployed SNS tokens are unaffected today. However, any future SNS that chooses a different decimal count (e.g. to mirror a 6-decimal ERC-20 asset or an 18-decimal one) will silently inherit this miscalculation. The entry path requires only a standard governance proposal submission, which is available to any SNS token holder with sufficient voting power.

---

### Recommendation

Replace the hardcoded `E8` divisor in `try_get_balance_valuation_factors` with the actual decimal count of the token being valued. The ICRC-1 ledger exposes `icrc1_decimals()` for this purpose. Concretely:

```rust
// Fetch decimals alongside balance and rates
let decimals_request = icrc1_client.icrc1_decimals();
let (balance_of_response, decimals_response, icps_per_token_response, xdrs_per_icp_response) =
    join!(balance_of_request, decimals_request, icps_per_token_request, xdrs_per_icp_request);

let decimals = decimals_response?;
let denominator = Decimal::from(10_u128.pow(decimals as u32));
let tokens = Decimal::from(u128::try_from(balance_of_response.0)?) / denominator;
```

The same fix must be applied consistently to `denominations_to_tokens` calls that convert `amount_e8s` to tokens in `proposal.rs` (lines 840, 1009, 2632, 2764), and to `tokens_to_e8s` in `treasury.rs` (line 55), so that both sides of every comparison use the same actual decimal count.

---

### Proof of Concept

1. Deploy an SNS with a token ledger initialised with `decimals = 6`.
2. Fund the SNS treasury with 1 000 000 tokens (= 10^12 smallest units). At a price of 0.5 ICP/token and 1 ICP/XDR, the treasury is worth 500 000 XDR — a **medium** treasury whose correct 7-day limit is 25 % = 250 000 tokens.
3. The code computes `tokens = 10^12 / 10^8 = 10 000`, XDR value = 5 000 → **small** treasury, limit = 100 % = 10 000 code-"tokens".
4. Submit a `TransferSnsTreasuryFunds` proposal with `amount_e8s = 3 × 10^11` (= 300 000 real tokens). The code computes `transfer_amount_tokens = 3 × 10^11 / 10^8 = 3 000 ≤ 10 000` → check passes.
5. The proposal is executed, transferring 300 000 tokens — 50 000 more than the intended 250 000-token limit.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L37-56)
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
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L177-181)
```rust
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
```

**File:** rs/nervous_system/common/src/lib.rs (L61-61)
```rust
pub const E8: u64 = 100_000_000;
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

**File:** rs/sns/governance/src/proposal.rs (L2632-2643)
```rust
    let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)
        // This Err cannot be provoked, because we are dividing a u64 (amount_e8s) by a positive
        // integer (E8).
        .ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::UnreachableCode,
                format!(
                    "Unable to convert proposals amount {} e8s to tokens.",
                    transfer.amount_e8s,
                ),
            )
        })?;
```

**File:** rs/sns/governance/src/proposal.rs (L2764-2771)
```rust
        let proposal_amount_tokens = denominations_to_tokens(proposal_amount_e8s, E8)
            // This Err is impossible, because we are dividing a u64 by a positive number.
            .ok_or_else(|| {
                format!(
                    "Failed to total amount in recent {proposal_type_description} proposals: \
                     Unable to convert amount {proposal_amount_e8s} e8s to whole tokens in proposal {proposal_id:?}.",
                )
            })?;
```

**File:** rs/sns/governance/src/treasury.rs (L54-65)
```rust
pub(crate) fn tokens_to_e8s(tokens: Decimal) -> Result<u64, String> {
    let e8s = tokens.checked_mul(Decimal::from(E8)).ok_or_else(|| {
        format!(
            "Unable to convert {tokens} tokens (Decimal) to e8s (u64) due to multiplication overflow.",
        )
    })?;

    let e8s = u64::try_from(e8s).map_err(|err| {
        format!("Unable to convert {tokens} tokens (Decimal) to e8s (u64): {err:?}",)
    })?;

    Ok(e8s)
```
