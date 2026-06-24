### Title
Spot ICP/XDR Rate Used for Cycles Minting Enables Rate-Jump Arbitrage - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using the **spot** `icp_xdr_conversion_rate` (updated every 5 minutes via heartbeat), not a time-weighted or smoothed average. An unprivileged user who observes a pending rate increase can submit an ICP-to-ledger transfer before the rate update, then call `notify_top_up` / `notify_mint_cycles` after the rate jumps, receiving more cycles per ICP than the pre-jump rate would have given. The ICP is burned at the old ledger-block value but cycles are minted at the new, higher rate, extracting value from the protocol.

### Finding Description
`tokens_to_cycles` in `rs/nns/cmc/src/main.rs` reads `state.icp_xdr_conversion_rate` — the **current spot rate** — at the moment `notify_top_up` or `notify_mint_cycles` is processed, not at the moment the ICP transfer was included in the ledger block.

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate   // ← spot rate at notification time
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        ...
    })
}
```

The spot rate is refreshed every **5 minutes** via `canister_heartbeat` → `update_exchange_rate` → `do_set_icp_xdr_conversion_rate`. Each heartbeat tick atomically replaces `state.icp_xdr_conversion_rate` with the latest value from the Exchange Rate Canister (XRC).

The notification flow is:
1. User sends ICP to CMC subaccount (ledger transfer, block recorded).
2. CMC heartbeat fires, XRC returns a higher rate, `icp_xdr_conversion_rate` is updated.
3. User calls `notify_top_up` (or `notify_mint_cycles`) referencing the old block index.
4. CMC calls `tokens_to_cycles(amount)` which reads the **new** spot rate.
5. User receives more cycles than the ICP was worth at transfer time.

The `blocks_notified` deduplication map prevents double-spending the same block, but it does **not** lock in the rate at transfer time. The notification can be deliberately delayed until after a known rate jump.

The CMC does maintain a 30-day `average_icp_xdr_conversion_rate`, but this is only used for governance/treasury valuation purposes (e.g., SNS token valuation via `get_average_icp_xdr_conversion_rate`). All three minting paths — `process_top_up`, `process_create_canister`, `process_mint_cycles` — call the same `tokens_to_cycles` which uses the spot rate exclusively.

### Impact Explanation
An attacker who can observe or predict a rate increase (e.g., by monitoring the XRC canister or knowing ICP price movements) can:
- Transfer ICP to the CMC subaccount just before the rate update.
- Delay calling `notify_top_up` / `notify_mint_cycles` until after the heartbeat fires with the new rate.
- Receive cycles computed at the higher rate while burning ICP at the lower pre-jump value.

The profit per operation is: `ICP_amount × (new_rate − old_rate) / old_rate × cycles_per_xdr`. For a 5-minute rate jump of 1% on a 150T-cycle-limit transaction, the gain is ~1.5T cycles (~$1.50 at current prices). The rate is updated every 5 minutes, so this is repeatable. The loss is borne by the protocol (fewer ICP burned per cycle minted than the current rate implies), diluting the ICP supply relative to cycles issued.

### Likelihood Explanation
The attack requires no privileged access. Any ICP holder can:
1. Monitor the XRC canister (public query) to anticipate rate changes.
2. Submit a ledger transfer.
3. Wait for the CMC heartbeat to update the rate.
4. Call `notify_top_up` or `notify_mint_cycles`.

The 5-minute update cadence is predictable and publicly observable. The `MAX_NOTIFY_HISTORY` window (1,000,000 blocks) gives ample time to delay notification. The per-transaction gain is modest but the attack is low-cost and repeatable by any ICP holder.

### Recommendation
Lock in the ICP/XDR conversion rate at the time the ICP ledger block was created, not at notification time. Concretely:
- Record the `icp_xdr_conversion_rate.timestamp_seconds` alongside the block index in `blocks_notified` when the block is first fetched.
- Use the rate whose `timestamp_seconds` is closest to (but not after) the ledger block's timestamp for the conversion.
- Alternatively, use the 30-day `average_icp_xdr_conversion_rate` (already computed and stored) for all minting conversions, which is far more resistant to 5-minute jumps.

### Proof of Concept
```
// Pseudocode — executable via any ICP wallet or canister

// Step 1: Observe current spot rate (e.g., 35_000 permyriad = 3.5 XDR/ICP)
let rate_before = cmc.get_icp_xdr_conversion_rate().data.xdr_permyriad_per_icp;

// Step 2: Transfer ICP to CMC subaccount with MEMO_TOP_UP_CANISTER
let block_index = ledger.transfer({
    to: cmc_subaccount(my_canister_id),
    amount: 100_ICP,
    memo: MEMO_TOP_UP_CANISTER,
});

// Step 3: Wait for CMC heartbeat to fire with higher rate
// (monitor XRC or simply wait ~5 minutes for a favorable tick)
// New rate: 35_350 permyriad (+1%)

// Step 4: Notify CMC — cycles computed at NEW rate
let cycles = cmc.notify_top_up({ block_index, canister_id: my_canister_id });
// cycles = 100 * 35_350/10_000 * cycles_per_xdr
// vs expected at old rate: 100 * 35_000/10_000 * cycles_per_xdr
// Profit: ~1% extra cycles on 100 ICP
```

**Root cause lines:** [1](#0-0) 

**Spot rate updated every 5 minutes via heartbeat:** [2](#0-1) [3](#0-2) 

**All three minting paths call the same spot-rate function:** [4](#0-3) [5](#0-4) [6](#0-5) 

**Average rate exists but is not used for minting:** [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/main.rs (L220-224)
```rust
    /// The average ICP/XDR rate over `NUM_DAYS_FOR_ICP_XDR_AVERAGE` days. The
    /// timestamp is the UNIX epoch time in seconds at the start of the last
    /// considered day, which should correspond to midnight of the current
    /// day.
    pub average_icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
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

**File:** rs/nns/cmc/src/main.rs (L1925-1932)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1958-1965)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1985-1991)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L2397-2402)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```
