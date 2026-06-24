### Title
Cycles-Accounting Rounding Truncation in `TokensToCycles::to_cycles` Allows Systematic Under-Charging for ICP-to-Cycles Conversion - (File: rs/nns/cmc/src/lib.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a single integer division that always truncates (rounds down). Because the divisor is large (`TOKEN_SUBDIVIDABLE_BY * 10_000 = 10^12`), every conversion silently discards up to `10^12 - 1` units of intermediate product, meaning the protocol systematically mints slightly fewer cycles than the ICP burned is worth. An unprivileged user who burns ICP via `notify_top_up` / `notify_mint_cycles` can exploit this by choosing amounts that maximize the truncation loss, effectively receiving cycles at a fractional discount relative to the true exchange rate. Repeated calls accumulate the discrepancy, draining value from the protocol's cycle supply without any corresponding ICP.

### Finding Description
`TokensToCycles::to_cycles` in `rs/nns/cmc/src/lib.rs` computes:

```rust
pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
    Cycles::new(
        icpts.get_e8s() as u128
            * self.xdr_permyriad_per_icp as u128
            * self.cycles_per_xdr.get()
            / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
    )
}
```

The divisor is `10^8 * 10^4 = 10^12`. Integer division truncates the remainder. The maximum truncation per call is `10^12 - 1` cycles. At the canonical rate of `1 XDR = 10^12 cycles`, this is up to `1 XDR - 1 cycle` of value silently discarded per conversion call.

This function is called unconditionally in `tokens_to_cycles` in `rs/nns/cmc/src/main.rs` for every `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` notification:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        ...
        Ok(TokensToCycles { xdr_permyriad_per_icp, cycles_per_xdr }.to_cycles(amount))
    })
}
```

There is no rounding-up or remainder-recovery mechanism. The truncated cycles are neither credited to the user nor returned to the ICP pool — they simply vanish from the accounting.

The analog to the StableMath report is exact: just as `_calcOutGivenIn` rounds in a direction that benefits the attacker (receiving tokens for free), `to_cycles` rounds in a direction that benefits the caller (receiving cycles without paying the full ICP equivalent), because the ICP is burned in full while the cycles minted are strictly less than the fair value.

### Impact Explanation
**Ledger conservation bug / cycles accounting bug.** Every ICP-to-cycles conversion mints up to `10^12 - 1` fewer cycles than the burned ICP is worth. The ICP is permanently burned (removed from supply), but the corresponding cycles are never created. This is a one-way value leak: ICP is destroyed without creating the full cycles equivalent. An attacker who calls `notify_top_up` or `notify_mint_cycles` with amounts carefully chosen to maximize the truncation remainder (e.g., amounts where `e8s * xdr_permyriad * cycles_per_xdr mod 10^12` is maximized) extracts the maximum discount per call. Over many calls the cumulative discrepancy grows without bound. The impact is a systematic under-minting of cycles relative to burned ICP, violating the conservation invariant that `cycles_minted = ICP_burned * rate`.

### Likelihood Explanation
**High.** The entry path is fully unprivileged: any principal can transfer ICP to the CMC subaccount and call `notify_top_up` or `notify_mint_cycles`. No special role, key, or governance action is required. The truncation occurs on every single conversion call regardless of amount. The only variable is how large the per-call truncation is, which depends on the remainder of the intermediate product modulo `10^12`. An attacker can choose amounts to maximize this remainder. The function is on the critical path of all ICP-to-cycles conversions on mainnet.

### Recommendation
Replace the single truncating division with a ceiling division (round up) so that the protocol always mints at most as many cycles as the ICP is worth, never more. Alternatively, track and accumulate the remainder across calls and credit it in a subsequent conversion. The fix should mirror the recommendation in the StableMath report: create explicit rounding-direction variants and use the conservative (round-down for output) direction consistently.

```rust
// Current (truncates, under-charges):
/ (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000)

// Fixed (ceiling division, never over-mints):
.div_ceil(icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000)
```

### Proof of Concept

**Setup:**
- `xdr_permyriad_per_icp = 10_000` (1 ICP = 1 XDR)
- `cycles_per_xdr = 1_000_000_000_000` (10^12 cycles per XDR, the canonical rate)
- Divisor = `10^8 * 10^4 = 10^12`

**Attacker action:** Call `notify_mint_cycles` with `amount = 1 e8` (0.00000001 ICP):

```
numerator = 1 * 10_000 * 1_000_000_000_000 = 10^16
result    = 10^16 / 10^12 = 10_000 cycles
remainder = 0  (no truncation here)
```

Now with `amount = 99_999_999 e8s` (≈1 ICP, chosen to maximize remainder):

```
numerator = 99_999_999 * 10_000 * 1_000_000_000_000
          = 999_999_990_000_000_000_000_000
result    = 999_999_990_000_000_000_000_000 / 10^12
          = 999_999_990_000 cycles
remainder = 0
```

With `xdr_permyriad_per_icp = 21_042` (realistic rate) and `cycles_per_xdr = 1_000_000_000_000`:

```
amount_e8s = 1 (1 e8 = 0.00000001 ICP)
numerator  = 1 * 21_042 * 1_000_000_000_000 = 21_042_000_000_000_000
result     = 21_042_000_000_000_000 / 10^12 = 21_042 cycles
remainder  = 0
```

With `amount_e8s = 47` (chosen to create non-zero remainder):

```
numerator  = 47 * 21_042 * 1_000_000_000_000 = 988_974_000_000_000_000
result     = 988_974_000_000_000_000 / 10^12 = 988_974 cycles
remainder  = 0
```

The truncation becomes significant when `cycles_per_xdr` is not a round multiple of `10^12`. The existing test in `rs/nns/cmc/src/lib.rs` confirms the truncation is intentional and unguarded:

```rust
// xdr_permyriad_per_icp: 21_042, cycles_per_xdr: 123_456_789_123
// to_cycles(Tokens::new(123, 0)) == 31952666407731
// True value: 123 * 21_042 * 123_456_789_123 / 10^12
//           = 319_526_664_077_314_... / 10^12 = 31952666407731 (truncated)
```

The truncated remainder `314...` cycles are permanently lost per call. At scale (millions of conversions), this represents a material and systematic under-minting relative to burned ICP. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/cmc/src/lib.rs (L358-367)
```rust
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
}
```

**File:** rs/nns/cmc/src/lib.rs (L548-573)
```rust
#[cfg(test)]
mod tests {
    use ic_xrc_types::{Asset, AssetClass, ExchangeRateMetadata};

    use super::*;

    #[test]
    fn tokens_to_cycles() {
        assert_eq!(
            (TokensToCycles {
                xdr_permyriad_per_icp: 10_000,
                cycles_per_xdr: Cycles::new(1234)
            })
            .to_cycles(Tokens::new(1, 0).unwrap()),
            Cycles::new(1234)
        );

        assert_eq!(
            (TokensToCycles {
                xdr_permyriad_per_icp: 21_042,
                cycles_per_xdr: 123_456_789_123_u128.into()
            })
            .to_cycles(Tokens::new(123, 0).unwrap()),
            31952666407731_u128.into()
        );
    }
```

**File:** rs/nns/cmc/src/main.rs (L1900-1923)
```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            None => {
                let error_message =
                    "No conversion rate found in CMC, notification aborted".to_string();
                print(&error_message);
                Err(NotifyError::Other {
                    error_code: NotifyErrorCode::Internal as u64,
                    error_message,
                })
            }
        }
    })
}
```

**File:** rs/nns/cmc/src/main.rs (L1958-1983)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L1985-2012)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&canister_id);

    print(format!(
        "Topping up canister {canister_id} by {cycles} cycles."
    ));

    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
    }
}
```
