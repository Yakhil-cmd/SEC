### Title
No Minimum Cycles Guard in CMC ICP-to-Cycles Conversion Endpoints - (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` endpoints convert ICP to cycles using the CMC's current `icp_xdr_conversion_rate` at notification time, with no mechanism for the caller to specify a minimum acceptable cycles output. Because the ICP ledger transfer (step 1) and the CMC notification (step 2) are two separate, independently committed transactions, the conversion rate can change between them. A user who sends ICP expecting a certain number of cycles may receive substantially fewer, with no recourse, because the ICP is burned unconditionally on a successful notification.

---

### Finding Description

The ICP-to-cycles conversion in the CMC is a two-step protocol:

**Step 1** – User sends ICP to a CMC subaccount on the ICP ledger (this transfer is final and irreversible once included in a block).

**Step 2** – User calls one of the three notification endpoints, referencing the block index of the transfer.

In step 2, the CMC calls `tokens_to_cycles(amount)`:

```rust
// rs/nns/cmc/src/main.rs:1899-1923
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
``` [1](#0-0) 

This uses whatever `icp_xdr_conversion_rate` is stored in CMC state at the moment of the call. The rate is refreshed every 5 minutes from the XRC canister.

None of the three notification argument types carry a `minimum_cycles` field:

- `NotifyTopUp { block_index, canister_id }` [2](#0-1) 
- `NotifyCreateCanister { block_index, controller, subnet_type, subnet_selection, settings }` [3](#0-2) 
- `NotifyMintCyclesArg { block_index, to_subaccount, deposit_memo }` [4](#0-3) 

The three processing functions (`process_top_up`, `process_create_canister`, `process_mint_cycles`) all call `tokens_to_cycles` and proceed unconditionally if a rate is available:

```rust
// rs/nns/cmc/src/main.rs:1985-2012
async fn process_top_up(...) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
    // no minimum check — cycles deposited at whatever rate is current
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await { ... }
}
``` [5](#0-4) 

The `TokensToCycles::to_cycles` computation is:

```rust
// rs/nns/cmc/src/lib.rs:358-367
pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
    Cycles::new(
        icpts.get_e8s() as u128
            * self.xdr_permyriad_per_icp as u128
            * self.cycles_per_xdr.get()
            / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
    )
}
``` [6](#0-5) 

If `xdr_permyriad_per_icp` drops between step 1 and step 2, the user receives proportionally fewer cycles. The ICP is burned regardless via `burn_and_log`. There is no rollback path once `deposit_cycles` / `do_mint_cycles` succeeds.

---

### Impact Explanation

A user who queries the current rate, sends ICP, and then calls a notification endpoint may receive materially fewer cycles than they planned for. Because the ICP is burned on success, the loss is permanent. For `notify_create_canister`, the canister may be created with fewer cycles than the minimum required for the user's intended workload. For `notify_top_up`, a canister may be topped up with fewer cycles than needed to avoid freezing. For `notify_mint_cycles`, the user's cycles-ledger balance is lower than expected. In all cases the user has no contractual floor to enforce.

---

### Likelihood Explanation

The CMC's `icp_xdr_conversion_rate` is updated every 5 minutes from the XRC canister. [7](#0-6)  In normal conditions the window between the ICP transfer and the notification is a few seconds, so the rate is unlikely to change. However:

- In volatile ICP market conditions the rate can shift several percent within a single 5-minute window.
- A user whose notification is delayed (network congestion, client-side retry logic, or deliberate waiting) is exposed to multiple rate updates.
- There is no deadline parameter on any notification endpoint, so a pending notification can be submitted arbitrarily late.
- The ICP is already locked in the CMC subaccount after step 1; the user cannot reclaim it without calling notify, so they are forced to accept whatever rate is current when they finally do call.

---

### Recommendation

Add an optional `minimum_cycles: Option<Cycles>` field to `NotifyTopUp`, `NotifyCreateCanister`, and `NotifyMintCyclesArg`. In `tokens_to_cycles` (or immediately after), check:

```rust
if let Some(min) = minimum_cycles {
    if cycles < min {
        return Err(NotifyError::Refunded {
            reason: format!("Computed cycles {} below caller minimum {}", cycles, min),
            block_index: refund_icp(...).await.ok(),
        });
    }
}
```

This mirrors the `require(borrowAmount <= maxBorrowAmount)` mitigation proposed in the external report and gives callers a slippage floor without breaking backward compatibility (the field is optional).

---

### Proof of Concept

1. User calls `get_icp_xdr_conversion_rate` on CMC; observes rate = 50 000 permyriad (5 XDR/ICP), `cycles_per_xdr` = 1 T. Expected yield for 10 ICP: **50 T cycles**.
2. User sends 10 ICP to the CMC top-up subaccount for canister C (ICP ledger transfer committed, irreversible).
3. Before the user calls `notify_top_up`, the CMC heartbeat fires and the XRC canister returns a new rate of 25 000 permyriad (2.5 XDR/ICP). CMC updates `icp_xdr_conversion_rate`.
4. User calls `notify_top_up { block_index, canister_id: C }`.
5. CMC calls `tokens_to_cycles(10 ICP)` → **25 T cycles** (half of expected).
6. `deposit_cycles(C, 25T, ...)` succeeds; `burn_and_log` burns the 10 ICP.
7. User's canister C receives 25 T cycles instead of the 50 T cycles the user budgeted for. The 10 ICP is gone. No minimum check existed to abort and refund. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1140-1145)
```rust
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

**File:** rs/nns/cmc/src/lib.rs (L126-130)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyTopUp {
    pub block_index: BlockIndex,
    pub canister_id: CanisterId,
}
```

**File:** rs/nns/cmc/src/lib.rs (L133-153)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize)]
pub struct NotifyCreateCanister {
    pub block_index: BlockIndex,

    /// If this not set to the caller's PrincipalId, notify_create_canister
    /// returns Err.
    ///
    /// Thus, notify_create_canister cannot be called on behalf of another
    /// principal. This might be surprising, but it is intentional.
    ///
    /// If controllers is not set in settings, controllers will be just
    /// [controller]. (Without this "default" behavior, the controller of the
    /// canister would be the Cycles Minting Canister itself.)
    pub controller: PrincipalId,

    #[deprecated(note = "use subnet_selection instead")]
    pub subnet_type: Option<String>,
    pub subnet_selection: Option<SubnetSelection>,

    pub settings: Option<CanisterSettings>,
}
```

**File:** rs/nns/cmc/src/lib.rs (L258-263)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
}
```

**File:** rs/nns/cmc/src/lib.rs (L351-367)
```rust
pub struct TokensToCycles {
    /// Number of 1/10,000ths of XDR that 1 ICP is worth.
    pub xdr_permyriad_per_icp: u64,
    /// Number of cycles that 1 XDR is worth.
    pub cycles_per_xdr: Cycles,
}

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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-17)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;

```
