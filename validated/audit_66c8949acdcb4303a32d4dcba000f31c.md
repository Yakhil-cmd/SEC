### Title
Missing Minimum Cycles Return Check in ICP-to-Cycles Conversion — (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles via a mandatory two-step flow: (1) transfer ICP to a CMC subaccount, then (2) call `notify_mint_cycles` or `notify_top_up`. Neither endpoint accepts a caller-specified minimum cycles parameter. The ICP/XDR conversion rate used at notification time is read from live CMC state, which is updated every five minutes via the `canister_heartbeat`. A user who queries the rate, transfers ICP, and then notifies may receive substantially fewer cycles than expected if the rate drops between steps — with no recourse, because the ICP is burned on success.

---

### Finding Description

The two-step ICP-to-cycles flow in the CMC is:

1. User queries `get_icp_xdr_conversion_rate` to learn the current rate.
2. User transfers ICP to the CMC's subaccount on the ICP ledger (irreversible once included in a block).
3. User calls `notify_mint_cycles` (or `notify_top_up`) referencing the block index.
4. CMC reads the **current** `icp_xdr_conversion_rate` from its live state and mints cycles accordingly.

The `NotifyMintCyclesArg` struct carries no minimum-cycles field: [1](#0-0) 

`process_mint_cycles` converts at whatever rate is live at notification time, with no floor check: [2](#0-1) 

`tokens_to_cycles` reads `icp_xdr_conversion_rate` directly from state: [3](#0-2) 

The rate is updated automatically every five minutes by the CMC heartbeat calling the exchange rate canister: [4](#0-3) 

The refresh interval is `5 * ONE_MINUTE_SECONDS`: [5](#0-4) 

The same gap exists for `notify_top_up`: [6](#0-5) 

---

### Impact Explanation

A user who observes rate R, transfers N ICP, and then calls `notify_mint_cycles` after the heartbeat has updated the rate to R′ < R will receive `N × R′` cycles instead of the expected `N × R` cycles. The ICP is burned on success — there is no refund path for a successful conversion. The user suffers a permanent, unrecoverable loss of cycles value with no protocol-level protection. For large ICP amounts or large rate swings, the loss can be material.

This is a **cycles/resource accounting bug**: the protocol does not preserve the user's economic intent expressed at transfer time.

---

### Likelihood Explanation

The rate is refreshed every five minutes via heartbeat. In volatile market conditions the ICP/XDR rate can move several percent within a single refresh window. The two-step flow is mandatory — there is no atomic single-call path — so every user of `notify_mint_cycles` or `notify_top_up` is exposed to this window. Any unprivileged principal can trigger the flow by sending ICP to the CMC subaccount and calling the notify endpoint. No special access is required.

---

### Recommendation

Add an optional `minimum_cycles: opt nat` field to `NotifyMintCyclesArg` and `NotifyTopUp`. In `process_mint_cycles` and `process_top_up`, after computing `cycles = tokens_to_cycles(amount)`, check:

```
if let Some(min) = minimum_cycles {
    if cycles < min {
        // refund ICP and return Err(NotifyError::Refunded { ... })
    }
}
```

This mirrors the pattern already used for `deposit_btc_min_amount` in the ckBTC minter: [7](#0-6) 

---

### Proof of Concept

1. User calls `get_icp_xdr_conversion_rate` and observes `xdr_permyriad_per_icp = 50_000` (1 ICP = 5 XDR = 5T cycles at `cycles_per_xdr = 1T`).
2. User transfers 100 ICP to the CMC subaccount, expecting 500T cycles.
3. CMC heartbeat fires; exchange rate canister returns a new rate of `xdr_permyriad_per_icp = 40_000`.
4. User calls `notify_mint_cycles` with the block index.
5. `tokens_to_cycles(100 ICP)` now returns 400T cycles.
6. CMC mints 400T cycles, burns 100 ICP. User receives 100T fewer cycles than expected with no refund and no error.

The `NotifyMintCyclesArg` accepted by the endpoint contains no field the user could have set to prevent this outcome: [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/lib.rs (L258-263)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
}
```

**File:** rs/nns/cmc/src/main.rs (L1900-1911)
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
```

**File:** rs/nns/cmc/src/main.rs (L1958-1966)
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
```

**File:** rs/nns/cmc/src/main.rs (L1985-1992)
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L272-283)
```rust
    let (deposit_btc_min_amount, check_fee) =
        read_state(|s| (s.deposit_btc_min_amount, s.check_fee));
    let mut utxo_statuses: Vec<UtxoStatus> = vec![];

    for utxo in processable_utxos {
        let ignored_reason = if utxo.value < deposit_btc_min_amount {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is lower than the minimum deposit amount {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(deposit_btc_min_amount)
            ))
```

**File:** rs/nns/cmc/cmc.did (L200-204)
```text
type NotifyMintCyclesArg = record {
  block_index : BlockIndex;
  to_subaccount : Subaccount;
  deposit_memo : Memo;
};
```
