### Title
No User-Specified Minimum Cycles Output in CMC ICP-to-Cycles Conversion — (`File: rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles via `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` using a cached, periodically-updated ICP/XDR exchange rate. None of these endpoints accept a caller-specified minimum cycles output. A user who sends ICP to the CMC subaccount and then calls `notify_top_up` (or the other notify endpoints) receives whatever the current cached rate produces at execution time, with no ability to abort if the rate has moved unfavorably since the ICP was sent. This is the direct IC analog of the ERC4626 vault slippage issue: an asset-conversion operation that changes the amount of a resource allocated to a user without any slippage guard.

---

### Finding Description

The two-step ICP-to-cycles flow is:

1. User sends ICP to a CMC subaccount (ledger `transfer`).
2. User calls `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` on the CMC.

In step 2, the CMC calls `tokens_to_cycles` which reads the cached `icp_xdr_conversion_rate` from state and multiplies:

```rust
// rs/nns/cmc/src/main.rs:1900-1922
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
            None => Err(...)
        }
    })
}
```

This result is used directly in `process_top_up`, `process_create_canister`, and `process_mint_cycles` without any caller-supplied minimum:

```rust
// rs/nns/cmc/src/main.rs:1985-2011
async fn process_top_up(...) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
    // cycles is used directly — no minimum check
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await { ... }
}
```

The `NotifyTopUp`, `NotifyCreateCanister`, and `NotifyMintCyclesArg` structs carry no `min_cycles_out` field:

```rust
// rs/nns/cmc/src/lib.rs:127-130
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}
```

The cached rate is updated every 5 minutes from the Exchange Rate Canister:

```rust
// rs/nns/cmc/src/exchange_rate_canister.rs:16
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

The gap between when the user sends ICP (step 1) and when they call notify (step 2) can span one or more rate refresh cycles. The rate can move significantly in either direction. The user has no on-chain mechanism to express "give me at least X cycles or revert."

The `treasury_manager.did` interface explicitly acknowledges this class of risk for the broader SNS treasury manager pattern:

```
// rs/sns/treasury_manager/treasury_manager.did:35-40
// Known Security Risks:
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

The CMC is the canonical production instance of exactly this pattern.

---

### Impact Explanation

A user who sends, say, 10 ICP to the CMC subaccount when the rate is 10 XDR/ICP (expecting 100T cycles) may call `notify_top_up` after a rate drop to 5 XDR/ICP and receive only 50T cycles — half the expected amount — with no recourse. The ICP is already burned. The user cannot retry with a different rate because the block is marked `NotifiedTopUp` and idempotently returns the same (unfavorable) result on any retry. The same applies to canister creation: a user expecting enough cycles to create a canister may receive fewer than `CREATE_CANISTER_MIN_CYCLES` worth of value at execution time, causing the creation to fail and triggering a refund minus the `CREATE_CANISTER_REFUND_FEE`. The financial loss is bounded by the magnitude of the rate swing between ICP transfer and notify call, which can be substantial over the multi-minute window.

---

### Likelihood Explanation

The ICP/XDR rate is updated every 5 minutes and can move several percent between updates. The two-step flow (transfer then notify) is the only supported path; there is no atomic single-step conversion. Any user who experiences network latency, wallet delays, or simply calls notify minutes after the transfer is exposed. The rate is publicly observable, so a sophisticated actor could time the notify call to coincide with a rate drop (though this requires no special privilege — any caller can call `notify_top_up` for any block they sent ICP for). This is a realistic, low-effort scenario for ordinary users and is not gated by any privileged role.

---

### Recommendation

Add an optional `min_cycles_out: Option<u128>` field to `NotifyTopUp`, `NotifyCreateCanister`, and `NotifyMintCyclesArg`. In `tokens_to_cycles` (or immediately after), check that the computed cycles meet the caller's minimum and return `NotifyError::Refunded` with a descriptive reason if not. This mirrors the standard slippage-protection pattern used in DEX swap interfaces and is consistent with the risk already documented in `treasury_manager.did`.

---

### Proof of Concept

1. User observes rate = 10 XDR/ICP → expects 100T cycles per ICP.
2. User calls `icp_ledger.transfer` sending 1 ICP to `CMC_subaccount(canister_id)` with `MEMO_TOP_UP_CANISTER`. Block index = B.
3. Rate drops to 5 XDR/ICP before user calls notify (within the 5-minute refresh window or across a refresh boundary).
4. User calls `cmc.notify_top_up({ block_index: B, canister_id: C })`.
5. CMC executes `tokens_to_cycles(1 ICP)` → reads cached `xdr_permyriad_per_icp = 50_000` (5 XDR/ICP) → returns 50T cycles.
6. `deposit_cycles(C, 50T, ...)` succeeds. ICP is burned. User receives 50T instead of 100T cycles.
7. Any retry of `notify_top_up` with block B returns the cached `NotifiedTopUp(Ok(50T))` result — the outcome is permanent.

No privileged access, no threshold attack, no social engineering required. The attacker-controlled entry path is the standard `notify_top_up` ingress call available to any principal. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1140-1162)
```rust
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
    let caller = caller();

    let src_canister_principal = SUBNET_RENTAL_CANISTER_ID.get();
    let limiter_to_use =
        if caller == src_canister_principal && canister_id.get() == src_canister_principal {
            // caller and destination needs to be src_canister_principal to get alternate limiter
            CyclesMintingLimiterSelector::SubnetRentalLimit
        } else {
            CyclesMintingLimiterSelector::BaseLimit
        };

    let (amount, from) = fetch_transaction(
        block_index,
        Subaccount::from(&canister_id),
        MEMO_TOP_UP_CANISTER,
    )
    .await?;
```

**File:** rs/nns/cmc/src/main.rs (L1900-1922)
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
```

**File:** rs/nns/cmc/src/main.rs (L1985-2011)
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
```

**File:** rs/nns/cmc/src/lib.rs (L127-130)
```rust
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```
