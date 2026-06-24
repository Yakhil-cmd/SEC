### Title
Lack of Slippage Protection in CMC ICP-to-Cycles Conversion Endpoints — (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a dynamic ICP/XDR exchange rate that is refreshed every five minutes. The three public notify endpoints — `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` — accept no minimum-cycles parameter. A user who queries the rate, sends ICP, and then submits a notification may receive materially fewer cycles than expected if the rate drops between those two steps, with no on-chain protection against the slippage.

### Finding Description
The CMC's ICP-to-cycles conversion is a two-step protocol:

1. The user queries `get_icp_xdr_conversion_rate` (a query call) to learn the current rate and calculates how many cycles their ICP will yield.
2. The user sends ICP to the CMC subaccount on the ICP ledger, then calls one of the notify endpoints (`notify_top_up`, `notify_create_canister`, or `notify_mint_cycles`).

Inside every notify path, `tokens_to_cycles` is called, which reads `state.icp_xdr_conversion_rate` at the moment the notification is processed:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        ...
        Ok(TokensToCycles { xdr_permyriad_per_icp, cycles_per_xdr: state.cycles_per_xdr }
            .to_cycles(amount))
    })
}
``` [1](#0-0) 

The rate stored in `state.icp_xdr_conversion_rate` is updated every five minutes by the exchange-rate canister heartbeat: [2](#0-1) 

None of the three notify argument types carry a `min_cycles` field:

- `NotifyTopUp` — only `block_index` + `canister_id` [3](#0-2) 

- `NotifyCreateCanister` — only `block_index`, `controller`, `subnet_selection`, `settings` [4](#0-3) 

- `NotifyMintCyclesArg` — only `block_index`, `to_subaccount`, `deposit_memo` [5](#0-4) 

The `TokensToCycles::to_cycles` conversion formula is:
```
cycles = e8s * xdr_permyriad_per_icp * cycles_per_xdr / (1e8 * 10_000)
``` [6](#0-5) 

If `xdr_permyriad_per_icp` drops between the user's query and the notification, the user receives fewer cycles than calculated, with no protocol-level recourse.

### Impact Explanation
**Cycles/resource accounting bug.** A user who pre-calculates the cycles yield of their ICP at rate R, then submits the notification after the rate has dropped to R′ < R, receives `amount × R′` cycles instead of the expected `amount × R` cycles. The shortfall is silently accepted — the ICP is burned and the reduced cycle amount is deposited. For `notify_create_canister`, if the resulting cycles fall below `CREATE_CANISTER_MIN_CYCLES` (100 B cycles), the canister creation fails and the ICP is refunded minus the `CREATE_CANISTER_REFUND_FEE`, causing a direct financial loss to the user. [7](#0-6) [8](#0-7) 

### Likelihood Explanation
The ICP/XDR rate is refreshed every five minutes. The two-step flow (ledger transfer → notify call) spans at least two IC rounds and can span several minutes if the user's client is slow or the network is congested. Historical ICP price data shows intra-day swings of several percent, meaning a 5-minute window is sufficient for a meaningful rate change. Any unprivileged user who calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` is exposed to this slippage with no opt-in protection available.

### Recommendation
Add an optional `min_cycles: Option<Cycles>` field to `NotifyTopUp`, `NotifyCreateCanister`, and `NotifyMintCyclesArg`. After computing `cycles = tokens_to_cycles(amount)`, check:

```rust
if let Some(min) = min_cycles {
    if cycles < min {
        // refund ICP and return Err(NotifyError::Refunded { reason: "slippage", ... })
    }
}
```

This mirrors the standard slippage-protection pattern used in AMM protocols and gives callers a deterministic guarantee about the minimum cycles they will receive.

### Proof of Concept
1. User calls `get_icp_xdr_conversion_rate` (query) and observes `xdr_permyriad_per_icp = 50_000` (5 XDR/ICP). With `cycles_per_xdr = 1_000_000_000_000` (1T cycles/XDR), 1 ICP yields 5T cycles.
2. User sends 10 ICP to the CMC top-up subaccount, expecting 50T cycles.
3. Before the user submits `notify_top_up`, the exchange-rate canister heartbeat fires and updates the rate to `xdr_permyriad_per_icp = 45_000` (4.5 XDR/ICP).
4. `notify_top_up` calls `tokens_to_cycles(10 ICP)` using the new rate: `10 × 45_000 × 1T / (1e8 × 10_000) = 45T cycles`.
5. The user receives 45T cycles instead of the expected 50T — a 10% shortfall — with no error, no warning, and no refund.

The entry path is the public `notify_top_up` update endpoint, reachable by any unprivileged ingress sender. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/nns/cmc/src/main.rs (L76-78)
```rust
/// Calls to create_canister get rejected outright if they have obviously too few cycles attached.
/// This is the minimum amount needed for creating a canister as of October 2023.
const CREATE_CANISTER_MIN_CYCLES: u64 = 100_000_000_000;
```

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

**File:** rs/nns/cmc/src/main.rs (L1239-1262)
```rust
async fn notify_mint_cycles(
    NotifyMintCyclesArg {
        block_index,
        to_subaccount,
        deposit_memo,
    }: NotifyMintCyclesArg,
) -> NotifyMintCyclesResult {
    let subaccount = Subaccount::from(&caller());
    let to_account = Account {
        owner: caller().into(),
        subaccount: to_subaccount,
    };

    let deposit_memo_len = deposit_memo.as_ref().map_or(0, |memo| memo.len());
    if deposit_memo_len > MAX_MEMO_LENGTH {
        return Err(NotifyError::Other {
            error_code: NotifyErrorCode::DepositMemoTooLong as u64,
            error_message: format!(
                "Memo length {deposit_memo_len} exceeds the maximum length of {MAX_MEMO_LENGTH}"
            ),
        });
    }

    let (amount, from) = fetch_transaction(block_index, subaccount, MEMO_MINT_CYCLES).await?;
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

**File:** rs/nns/cmc/src/main.rs (L1943-1955)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
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
