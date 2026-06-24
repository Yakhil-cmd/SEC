### Title
ICP-to-Cycles Conversion Integer Truncation Causes Systematic User Value Loss in CMC - (File: rs/nns/cmc/src/lib.rs)

### Summary
The `TokensToCycles::to_cycles()` function in the Cycles Minting Canister (CMC) converts ICP to cycles using integer division. The full ICP amount is burned from the user's ledger account, but the cycles minted are truncated (rounded down). The "dust" cycles — up to `TOKEN_SUBDIVIDABLE_BY × 10_000 − 1 = 999_999_999_999` cycles per transaction — are never minted, causing a systematic, unrecoverable loss of value for every user who converts ICP to cycles.

### Finding Description

In `rs/nns/cmc/src/lib.rs`, the `TokensToCycles::to_cycles()` function is:

```rust
pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
    Cycles::new(
        icpts.get_e8s() as u128
            * self.xdr_permyriad_per_icp as u128
            * self.cycles_per_xdr.get()
            / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
    )
}
``` [1](#0-0) 

The divisor is `TOKEN_SUBDIVIDABLE_BY × 10_000 = 1e8 × 1e4 = 1e12`. Rust integer division truncates toward zero, so the user receives `floor(e8s × xdr_permyriad_per_icp × cycles_per_xdr / 1e12)` cycles. The remainder — up to `1e12 − 1` cycles — is silently discarded and never minted.

This function is called in three user-facing flows inside `rs/nns/cmc/src/main.rs`:

- `process_top_up` (triggered by `notify_top_up`): burns the full ICP amount, deposits only the truncated cycle count.
- `process_create_canister` (triggered by `notify_create_canister`): same pattern.
- `process_mint_cycles` (triggered by `notify_mint_cycles`): same pattern. [2](#0-1) 

In every case, `burn_and_log(sub, amount).await` burns the **full** ICP amount, while only the truncated cycle count is credited: [3](#0-2) 

The `tokens_to_cycles` helper that feeds all three paths: [4](#0-3) 

### Impact Explanation

Every ICP-to-cycles conversion permanently destroys up to `1e12 − 1` cycles of value per transaction. At the canonical rate of `cycles_per_xdr = 1_000_000_000_000` (1 T cycles per XDR) and 1 XDR ≈ \$1, the maximum per-transaction loss is approximately \$1 in cycles that are never minted. The ICP is fully burned; the unissued cycles accumulate nowhere — they are simply never created. The loss is:

- **Systematic**: every conversion rounds down, never up.
- **Unrecoverable**: the burned ICP cannot be refunded and the missing cycles are never issued.
- **Unbounded in aggregate**: with millions of conversions, the total destroyed value grows without limit.

### Likelihood Explanation

High. The three notification endpoints (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) are publicly callable by any unprivileged ingress sender. Every real-world ICP-to-cycles conversion goes through `to_cycles()`, so every user is affected on every call. No special role, key, or governance majority is required. [5](#0-4) 

### Recommendation

Replace the floor division with a computation that does not silently destroy value. Two options:

1. **Ceiling division** — ensure the user receives at least

### Citations

**File:** rs/nns/cmc/src/lib.rs (L358-366)
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
```

**File:** rs/nns/cmc/src/main.rs (L1139-1145)
```rust
#[update]
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
```

**File:** rs/nns/cmc/src/main.rs (L1899-1923)
```rust
// If conversion fails, log and return an error
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
