### Title
Lack of Slippage Protection in CMC `notify_top_up` and `notify_mint_cycles` — (`rs/nns/cmc/src/main.rs`, `rs/nns/cmc/src/lib.rs`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles via a two-step flow: the user first sends ICP to a CMC subaccount (irrevocably committing funds), then calls `notify_top_up` or `notify_mint_cycles` to trigger the conversion. Neither call accepts a `min_cycles_out` parameter. The conversion uses the current `icp_xdr_conversion_rate` at execution time, which is updated every five minutes from the Exchange Rate Canister. If the rate drops between the ICP transfer and the notification call, the user receives fewer cycles than expected with no ability to revert.

---

### Finding Description

The ICP-to-cycles conversion flow in the CMC is a two-step protocol:

**Step 1 — ICP transfer (irrevocable):** The user sends ICP to a CMC-controlled subaccount on the ICP ledger. Once confirmed, these funds are committed.

**Step 2 — Notification (no slippage guard):** The user calls `notify_top_up` or `notify_mint_cycles` with only the ledger block index and a destination. Neither argument type carries a minimum cycles output field.

`NotifyTopUpArg` contains only `block_index` and `canister_id`: [1](#0-0) 

`NotifyMintCyclesArg` contains only `block_index`, `to_subaccount`, and `deposit_memo`: [2](#0-1) 

Both notification handlers delegate to `tokens_to_cycles`, which reads the current `icp_xdr_conversion_rate` from state at the moment of execution: [3](#0-2) 

The conversion arithmetic is: [4](#0-3) 

`process_mint_cycles` and `process_top_up` call `tokens_to_cycles` with no floor check on the resulting cycles: [5](#0-4) [6](#0-5) 

The ICP/XDR rate is refreshed every five minutes from the Exchange Rate Canister: [7](#0-6) 

---

### Impact Explanation

A user who sends ICP in step 1 is fully committed: the ICP sits in the CMC subaccount and there is no automatic refund path if the user simply does not call `notify_top_up`. The user must eventually call the notification to recover value. If the ICP/XDR rate has fallen between the transfer and the notification, the user receives fewer cycles than they anticipated when they initiated the transfer, with no on-chain mechanism to enforce a minimum acceptable output. During periods of ICP price volatility, the shortfall can be material (e.g., a 10–20% rate drop within a single five-minute window is plausible during high-volatility events).

---

### Likelihood Explanation

The ICP/XDR rate is updated every five minutes from the Exchange Rate Canister. Any unprivileged principal can call `notify_top_up` or `notify_mint_cycles` after sending ICP. The window between the ICP transfer (which requires at least one ledger finalization round) and the notification call is non-zero and can span one or more rate updates. Rate drops of several percent within a single update cycle have occurred historically. No privileged access, key compromise, or consensus attack is required; the vulnerability is triggered by normal user interaction under adverse market conditions.

---

### Recommendation

Add an optional `min_cycles_out: opt nat` field to both `NotifyTopUpArg` and `NotifyMintCyclesArg`. In `process_top_up` and `process_mint_cycles`, after calling `tokens_to_cycles`, compare the result against `min_cycles_out`; if the computed cycles fall below the caller's threshold, refund the ICP (minus the standard refund fee) and return a new `NotifyError::SlippageExceeded` variant. This mirrors the EIP-4626 recommendation of a `minSharesOut` guard and gives callers a trustless way to bound their worst-case conversion outcome.

---

### Proof of Concept

1. Observe the current ICP/XDR rate via `get_icp_xdr_conversion_rate` (e.g., rate = 10 000 XDR/ICP → 1 ICP ≈ 10 T cycles).
2. Send 1 ICP to the CMC subaccount for `MEMO_MINT_CYCLES`. The transfer is finalized on the ICP ledger.
3. Wait for the next Exchange Rate Canister update (up to 5 minutes). If the rate drops to, say, 8 000 XDR/ICP, the CMC state now reflects the lower rate.
4. Call `notify_mint_cycles` with `NotifyMintCyclesArg { block_index, to_subaccount: None, deposit_memo: None }`.
5. `tokens_to_cycles` reads the new rate and mints ≈ 8 T cycles instead of the expected 10 T cycles. The caller has no recourse; the ICP is burned and the shortfall is unrecoverable.

The entry path is a standard unprivileged ingress call sequence reachable by any principal, with no admin keys, governance majority, or threshold-crypto involvement required. [8](#0-7) [9](#0-8)

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

**File:** rs/nns/cmc/src/lib.rs (L258-263)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub struct NotifyMintCyclesArg {
    pub block_index: BlockIndex,
    pub to_subaccount: Option<icrc_ledger_types::icrc1::account::Subaccount>,
    pub deposit_memo: Option<Vec<u8>>,
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```
