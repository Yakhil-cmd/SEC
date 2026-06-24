### Title
Lack of Slippage Protection in CMC `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` — (`rs/nns/cmc/src/main.rs`, `rs/nns/cmc/cmc.did`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles via a mandatory two-step process: (1) transfer ICP to a CMC subaccount on the ICP ledger, then (2) call `notify_top_up` / `notify_mint_cycles` / `notify_create_canister`. The conversion uses the ICP/XDR rate stored in CMC state **at the time of the notify call**, which is refreshed every 5 minutes. Neither the `NotifyTopUpArg` nor the `NotifyMintCyclesArg` nor `NotifyCreateCanisterArg` accept a `min_cycles_out` parameter. Once ICP is committed to the CMC subaccount, the user has no mechanism to abort or set a floor on the cycles they receive, and no automatic refund path exists for unclaimed ICP in the subaccount.

---

### Finding Description

The CMC's ICP-to-cycles conversion is split into two on-chain steps. In step 1, the user sends ICP to a CMC-controlled subaccount (keyed by `canister_id`). In step 2, the user calls `notify_top_up` (or the analogous `notify_mint_cycles` / `notify_create_canister`), which triggers `process_top_up` → `tokens_to_cycles`:

```rust
// rs/nns/cmc/src/main.rs
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
            ...
        }
    })
}
```

The rate used is whatever `icp_xdr_conversion_rate` is stored in CMC state at the moment `notify_top_up` executes. This rate is updated approximately every 5 minutes (`REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS`). The `NotifyTopUpArg` Candid type contains only `block_index` and `canister_id` — no `min_cycles_out` field:

```candid
// rs/nns/cmc/cmc.did
type NotifyTopUpArg = record {
  block_index : BlockIndex;
  canister_id : principal;
};
```

Once ICP is in the CMC subaccount, the user cannot retrieve it without completing the notify flow. There is no `error_refund_icp`-style escape hatch for the CMC (unlike the SNS Swap canister). If the ICP/XDR rate drops between step 1 and step 2, the user receives fewer cycles than they observed when they initiated the transfer, with no recourse.

---

### Impact Explanation

A user who queries `get_icp_xdr_conversion_rate` before sending ICP, observes a favorable rate, sends ICP, and then calls `notify_top_up` after a rate update will receive fewer cycles than expected. The ICP is burned and cycles are minted at the unfavorable rate. The user cannot specify a minimum acceptable cycles amount, cannot abort, and cannot recover the ICP. The loss is permanent and proportional to the rate drop. For large top-ups (e.g., subnet rental, which uses a separate higher limit), the absolute loss in cycles value can be significant.

---

### Likelihood Explanation

The ICP/XDR rate is updated every ~5 minutes from the Exchange Rate Canister. The two-step process (transfer + notify) is inherent to the CMC design and always introduces a window of rate exposure. ICP price is volatile. Any user performing a large top-up during a period of ICP price decline will silently receive fewer cycles than they planned for. This is a realistic, recurring scenario requiring no special attacker capability — the "attacker" is simply adverse market movement between the two mandatory steps.

---

### Recommendation

Add an optional `min_cycles_out : opt nat` field to `NotifyTopUpArg`, `NotifyMintCyclesArg`, and `NotifyCreateCanisterArg`. In `process_top_up` / `process_mint_cycles` / `process_create_canister`, after computing `cycles = tokens_to_cycles(amount)?`, check:

```rust
if let Some(min_out) = min_cycles_out {
    if cycles < min_out {
        let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
        return Err(NotifyError::Refunded {
            reason: format!("cycles {} below minimum {}", cycles, min_out),
            block_index: refund_block,
        });
    }
}
```

This preserves backward compatibility (the field is optional) and gives callers the same protection that EIP-4626 recommends for vault withdrawals.

---

### Proof of Concept

1. Alice queries `get_icp_xdr_conversion_rate` and sees `xdr_permyriad_per_icp = 100_000` (10 XDR/ICP). At `cycles_per_xdr = 1_000_000_000_000`, 1 ICP → 10T cycles.
2. Alice transfers 10 ICP to the CMC subaccount for her canister on the ICP ledger.
3. Before Alice calls `notify_top_up`, the XRC updates the CMC rate to `xdr_permyriad_per_icp = 80_000` (8 XDR/ICP).
4. Alice calls `notify_top_up { block_index, canister_id }`.
5. `tokens_to_cycles(10 ICP)` computes `10 * 8 * 1_000_000_000_000 = 80T cycles` instead of the expected 100T.
6. Alice's canister receives 80T cycles; 20T cycles of value is silently lost. The ICP is burned. There is no error, no warning, and no refund.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/cmc/cmc.did (L27-33)
```text
type NotifyTopUpArg = record {
  // Index of the block on the ICP ledger that contains the payment.
  block_index : BlockIndex;

  // The canister to top up.
  canister_id : principal;
};
```

**File:** rs/nns/cmc/src/main.rs (L1140-1145)
```rust
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```
