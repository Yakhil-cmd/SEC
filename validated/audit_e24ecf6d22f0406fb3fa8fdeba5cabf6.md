Audit Report

## Title
`AmountTooLow` error path permanently burns user ckBTC without reimbursement when UTXO pool is fragmented — (`rs/bitcoin/ckbtc/minter/src/lib.rs`, `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

## Summary

`fee_based_minimum_withdrawal_amount` computes the minimum safe withdrawal using a hardcoded single-input vsize bound (`PER_REQUEST_VSIZE_BOUND = 221`). When the minter's UTXO pool is fragmented into many small UTXOs, the greedy selection algorithm picks far more inputs, making the actual transaction fee exceed the withdrawal amount and triggering `BuildTxError::AmountTooLow`. The `AmountTooLow` branch in `submit_pending_requests` finalizes the request with no reimbursement — unlike the `InvalidTransaction` branch which calls `reimburse_canceled_requests` — so the user's ckBTC is permanently burned with no BTC sent and no refund issued.

## Finding Description

**Root cause — single-input vsize assumption:**
`fee_based_minimum_withdrawal_amount` uses `PER_REQUEST_VSIZE_BOUND = 221` vbytes, modeling one P2WPKH input + two outputs. [1](#0-0) 

Each additional P2WPKH input adds ~68 vbytes. With N inputs, the real vsize is approximately `68N + 105` vbytes, which can be orders of magnitude larger than 221 vbytes.

**Burn before transaction build:**
In `retrieve_btc`, the guard checks only `args.amount >= fee_based_retrieve_btc_min_amount`, then calls `burn_ckbtcs` at line 210 — before any transaction is constructed or UTXO selection is attempted. [2](#0-1) 

**Greedy UTXO selection with fragmented pool:**
`greedy` calls `find_lower_bound(goal)` to find the smallest UTXO ≥ goal, falling back to `available_utxos.last()` (the largest available) when none qualifies. With 100 UTXOs of 2,001 sats each and a 200,000-sat target, it selects all 100 UTXOs. [3](#0-2) 

**Fee computed from real vsize:**
`evaluate_transaction_fee` calls `fake_sign(tx).vsize()` on the fully-constructed transaction with all N inputs. At `fee_rate = 343,000` millisat/vbyte with 100 inputs: vsize ≈ 6,905 vbytes → fee ≈ 2,368,415 sats >> 200,000 sats → `BuildTxError::AmountTooLow`. [4](#0-3) 

**No reimbursement on `AmountTooLow`:**
The `AmountTooLow` branch calls only `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow`. No reimbursement is scheduled. [5](#0-4) 

`remove_retrieve_btc_request` only records an event and pushes to `finalized_requests` — no mint-back, no reimbursement queue entry. [6](#0-5) 

**Contrast with `InvalidTransaction` path which does reimburse:** [7](#0-6) 

`FinalizedStatus` has no reimbursement variant — `AmountTooLow` is a dead-end terminal state. [8](#0-7) 

## Impact Explanation

A user who submits a withdrawal at exactly `fee_based_retrieve_btc_min_amount` loses their ckBTC permanently when the minter's UTXO pool is fragmented: the ckBTC ledger burn is irreversible, no BTC transaction is sent, and the `AmountTooLow` terminal state carries no reimbursement path. This constitutes a concrete permanent loss of ck-token assets, matching the **High** impact class: *"Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm."* The per-victim loss is bounded by the minimum withdrawal amount (~200,000 sats ≈ ~$200 at current prices), but the attack is repeatable against any user withdrawing at the minimum while the pool remains fragmented.

## Likelihood Explanation

Any unprivileged user can fragment the minter's UTXO pool by depositing BTC in small amounts (each ≥ `deposit_btc_min_amount` ≈ 2,001 sats). The consolidation mechanism only triggers when `available_utxos.len() > UTXOS_COUNT_THRESHOLD` (line 1049), so with ~100 small UTXOs (well below the threshold), consolidation does not run. The attacker's cost (~200,100 sats + on-chain fees) is comparable to each victim's loss, making this a griefing vector. The fragmented state persists until consolidation runs, meaning multiple victims can be affected from a single fragmentation setup. The precondition (fragmented pool) is achievable without any special privileges. [9](#0-8) 

## Recommendation

1. **Reimbursement parity (minimal fix):** The `AmountTooLow` branch in `submit_pending_requests` should call `reimburse_canceled_requests` (minus a small processing fee), matching the behavior of the `InvalidTransaction` branch. This prevents permanent fund loss regardless of the root cause.

2. **Multi-input-aware minimum:** `fee_based_minimum_withdrawal_amount` should account for the worst-case number of inputs the greedy algorithm might select for the given amount (e.g., `ceil(amount / min_utxo_value)` inputs), or use a conservative upper bound.

3. **Lower consolidation threshold:** Reduce `UTXOS_COUNT_THRESHOLD` so fragmentation is addressed before it can affect minimum-amount withdrawals.

## Proof of Concept

```rust
// Fragment the pool: 100 UTXOs of 2_001 sats each
let mut utxos: UtxoSet = (0..100u32)
    .map(|i| Utxo {
        outpoint: OutPoint { txid: [i as u8; 32].into(), vout: 0 },
        value: 2_001,
        height: 10,
    })
    .collect();

let fee_rate = FeeRate::from_millis_per_byte(343_000);
let fee_estimator = BitcoinFeeEstimator::new(Network::Mainnet, 100_000, 2_000);

// Minimum amount at this fee rate = 200_000 sats
let min_amount = fee_estimator.fee_based_minimum_withdrawal_amount(fee_rate);
assert_eq!(min_amount, 200_000);

// Build transaction at exactly the minimum — should succeed per the invariant
let result = build_unsigned_transaction(
    &mut utxos,
    vec![(BitcoinAddress::P2wpkhV0([1; 20]), min_amount)],
    &BitcoinAddress::P2wpkhV0([0; 20]),
    DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION,
    fee_rate,
    &fee_estimator,
);

// FAILS: Err(BuildTxError::AmountTooLow)
// 100 inputs × ~68 vbytes = ~6905 vbytes → fee ≈ 2.37M sats >> 200_000 sats
// ckBTC already burned at this point — no reimbursement issued
assert!(result.is_ok(), "invariant violated: {:?}", result);
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L133-144)
```rust
                const PER_REQUEST_RBF_BOUND: u64 = 22_100;
                const PER_REQUEST_VSIZE_BOUND: u64 = 221;
                const PER_REQUEST_MINTER_FEE_BOUND: u64 = 305;

                ((PER_REQUEST_RBF_BOUND
                    + median_fee_rate.fee_ceil(PER_REQUEST_VSIZE_BOUND)
                    + PER_REQUEST_MINTER_FEE_BOUND
                    + self.check_fee)
                    / 50_000) //TODO DEFI-2187: adjust increment of minimum withdrawal amount to be a multiple of retrieve_btc_min_amount/2
                    * 50_000
                    + self.retrieve_btc_min_amount
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L166-210)
```rust
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }

    let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }

    let balance = balance_of(caller).await?;
    if args.amount > balance {
        return Err(RetrieveBtcError::InsufficientFunds { balance });
    }

    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
    let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
    match status {
        BtcAddressCheckStatus::Tainted => {
            log!(
                Priority::Debug,
                "rejected an attempt to withdraw {} BTC to address {} due to failed Bitcoin check",
                crate::tx::DisplayAmount(args.amount),
                args.address,
            );
            return Err(RetrieveBtcError::GenericError {
                error_message: "Destination address is tainted".to_string(),
                error_code: ErrorCode::TaintedAddress as u64,
            });
        }
        BtcAddressCheckStatus::Clean => {}
    }

    let burn_memo = BurnMemo::Convert {
        address: Some(&args.address),
        kyt_fee: None,
        status: Some(Status::Accepted),
    };
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L400-411)
```rust
            Err(BuildTxError::InvalidTransaction(err)) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: error in building transaction ({:?})",
                    err
                );
                let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
                let reimbursement_fee = fee_estimator
                    .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
                reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
                None
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L412-434)
```rust
            Err(BuildTxError::AmountTooLow) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: dropping requests for total BTC amount {} to addresses {} (too low to cover the fees)",
                    tx::DisplayAmount(batch.iter().map(|req| req.amount).sum::<u64>()),
                    batch
                        .iter()
                        .map(|req| req.address.display(s.btc_network))
                        .collect::<Vec<_>>()
                        .join(",")
                );

                // There is no point in retrying the request because the
                // amount is too low.
                for request in batch {
                    state::audit::remove_retrieve_btc_request(
                        s,
                        request,
                        state::FinalizedStatus::AmountTooLow,
                        runtime,
                    );
                }
                None
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1049-1057)
```rust
    if available_utxos.len() > UTXOS_COUNT_THRESHOLD {
        while input_utxos.len() < output_count + 1 {
            if let Some(min_utxo) = available_utxos.pop_first() {
                input_utxos.push(min_utxo);
            } else {
                break;
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1070-1099)
```rust
fn greedy(target: u64, available_utxos: &mut UtxoSet) -> Vec<Utxo> {
    #[cfg(feature = "canbench-rs")]
    let _scope = canbench_rs::bench_scope("greedy");

    let mut solution = vec![];
    let mut goal = target;
    while goal > 0 {
        let candidate_utxo = available_utxos
            .find_lower_bound(goal)
            .or_else(|| available_utxos.last())
            .cloned();
        match candidate_utxo {
            Some(utxo) => {
                let utxo = available_utxos.remove(&utxo).expect("BUG: missing UTXO");
                goal = goal.saturating_sub(utxo.value);
                solution.push(utxo);
            }
            None => {
                // Not enough available UTXOs to satisfy the request.
                for u in solution {
                    available_utxos.insert(u);
                }
                return vec![];
            }
        }
    }

    debug_assert!(solution.is_empty() || solution.iter().map(|u| u.value).sum::<u64>() >= target);

    solution
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1304-1308)
```rust
    let fee = fee_estimator.evaluate_transaction_fee(&unsigned_tx, fee_rate);

    if fee + minter_fee > amount {
        return Err(BuildTxError::AmountTooLow);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L67-84)
```rust
pub fn remove_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    status: FinalizedStatus,
    runtime: &R,
) {
    record_event(
        EventType::RemovedRetrieveBtcRequest {
            block_index: request.block_index,
        },
        runtime,
    );

    state.push_finalized_request(FinalizedBtcRequest {
        request: request.into(),
        state: status,
    });
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L259-267)
```rust
pub enum FinalizedStatus {
    /// The request amount was to low to cover the fees.
    AmountTooLow,
    /// The transaction that retrieves BTC got enough confirmations.
    Confirmed {
        /// The witness transaction identifier of the transaction.
        txid: Txid,
    },
}
```
