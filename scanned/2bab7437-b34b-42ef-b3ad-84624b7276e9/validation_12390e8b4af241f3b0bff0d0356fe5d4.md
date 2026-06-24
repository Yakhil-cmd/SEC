### Title
Lack of Slippage Protection in ICP-to-Cycles Conversion — (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) exposes three ICP-to-cycles conversion endpoints — `notify_top_up`, `notify_mint_cycles`, and `notify_create_canister` — none of which accept a caller-specified minimum cycles output. The ICP/XDR conversion rate used at execution time is the live rate stored in CMC state, which is updated every ~5 minutes from the exchange rate canister. Because the two-step flow (send ICP → call notify) spans an unbounded time window, a user's ICP can be burned at a materially worse rate than they observed when initiating the transfer, with no ability to revert.

---

### Finding Description

The ICP-to-cycles conversion flow in the CMC is a two-step process:

1. The user sends ICP to a CMC subaccount via the ICP ledger.
2. The user calls `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister`, referencing the ledger block index.

The actual conversion is performed inside `tokens_to_cycles` at execution time of step 2:

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
            ...
        }
    })
}
```

This function reads `state.icp_xdr_conversion_rate` — the live rate — with no reference to any user-supplied minimum. The three notify argument types contain no such field:

- `NotifyTopUp { block_index, canister_id }` — no `min_cycles`
- `NotifyMintCyclesArg { block_index, to_subaccount, deposit_memo }` — no `min_cycles`
- `NotifyCreateCanister { block_index, controller, subnet_selection, settings }` — no `min_cycles`

Once `notify_top_up` or `notify_mint_cycles` succeeds, `burn_and_log` is called and the ICP is permanently destroyed. There is no post-hoc recourse for the user.

The rate is updated approximately every 5 minutes via the exchange rate canister heartbeat path. A user who observes a rate of R at the time of the ICP transfer may receive cycles computed at rate R′ < R if the rate drops before they call notify. The gap between step 1 and step 2 can be arbitrarily large (e.g., network congestion, user delay, wallet UX latency).

---

### Impact Explanation

A user who sends N ICP expecting at least C cycles (based on the rate they observed) may receive C′ < C cycles, with the difference silently absorbed. The ICP is burned regardless. For large ICP amounts or during periods of high ICP price volatility, the shortfall can be economically significant. The user has no mechanism to express a minimum acceptable output and no ability to recover the ICP once the notify call succeeds.

This is a **cycles/resource accounting bug**: the protocol permanently destroys a user's ICP at a rate the user did not consent to, with no slippage guard.

---

### Likelihood Explanation

The ICP/XDR rate is updated every ~5 minutes. Any user who does not call notify within the same ~5-minute window as their ICP transfer is exposed. In practice, wallet UIs, scripted flows, and congested network conditions routinely introduce delays longer than 5 minutes between the ledger transfer and the notify call. The rate can move by several percent within a single update cycle during volatile market conditions. This is a realistic, low-effort scenario for any unprivileged ingress sender.

---

### Recommendation

Add an optional `min_cycles: Option<Nat>` field to `NotifyTopUpArg`, `NotifyMintCyclesArg`, and `NotifyCreateCanisterArg`. After computing `cycles = tokens_to_cycles(amount)`, check:

```rust
if let Some(min) = min_cycles {
    if cycles < min {
        // refund ICP and return slippage error
    }
}
```

This is backward-compatible (the field is optional) and mirrors the standard slippage-protection pattern used in DeFi deposit functions.

---

### Proof of Concept

1. Observe the current ICP/XDR rate via `get_icp_xdr_conversion_rate` — e.g., 10,000 XDR/ICP → 1 ICP = 1,000,000,000,000 cycles.
2. Send 10 ICP to the CMC subaccount for canister `C` via the ICP ledger (step 1 of the flow).
3. Wait >5 minutes. The exchange rate canister updates the CMC rate to 8,000 XDR/ICP.
4. Call `notify_top_up { block_index: <step-2-block>, canister_id: C }`.
5. `tokens_to_cycles(10 ICP)` now computes 800,000,000,000 cycles instead of the expected 1,000,000,000,000.
6. `burn_and_log` destroys the 10 ICP. Canister `C` receives 200T fewer cycles than the user expected, with no error and no refund.

**Relevant code locations:**

- `notify_top_up` entry point: [1](#0-0) 
- `notify_mint_cycles` entry point: [2](#0-1) 
- `tokens_to_cycles` — live rate used, no minimum check: [3](#0-2) 
- `process_top_up` — calls `tokens_to_cycles` then burns ICP: [4](#0-3) 
- `process_mint_cycles` — same pattern: [5](#0-4) 
- `NotifyTopUp` struct — no `min_cycles` field: [6](#0-5) 
- `NotifyMintCyclesArg` struct — no `min_cycles` field: [7](#0-6) 
- CMC DID interface confirming no minimum parameter in any notify call: [8](#0-7)

### Citations

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

**File:** rs/nns/cmc/src/main.rs (L1238-1245)
```rust
#[update]
async fn notify_mint_cycles(
    NotifyMintCyclesArg {
        block_index,
        to_subaccount,
        deposit_memo,
    }: NotifyMintCyclesArg,
) -> NotifyMintCyclesResult {
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

**File:** rs/nns/cmc/src/lib.rs (L126-130)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}
```

**File:** rs/nns/cmc/src/lib.rs (L257-263)
```rust
/// Argument taken by `notify_mint_cycles` endpoint
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
}
```

**File:** rs/nns/cmc/cmc.did (L200-218)
```text
type NotifyMintCyclesArg = record {
  block_index : BlockIndex;
  to_subaccount : Subaccount;
  deposit_memo : Memo;
};

type NotifyMintCyclesResult = variant {
  Ok : NotifyMintCyclesSuccess;
  Err : NotifyError;
};

type NotifyMintCyclesSuccess = record {
  // Cycles ledger block index of deposit
  block_index : nat;
  // Amount of cycles that were minted and deposited to the cycles ledger
  minted : nat;
  // New balance of the cycles ledger account
  balance : nat;
};
```
