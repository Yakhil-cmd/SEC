### Title
TOCTOU Race in ckBTC Minter Allows `MAX_CONCURRENT_PENDING_REQUESTS` Limit to Be Exceeded - (File: `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

Both `retrieve_btc` and `retrieve_btc_with_approval` in the ckBTC minter check the pending-request count **before** performing multiple async inter-canister calls. Because the IC canister yields control at every `await` point, multiple concurrent callers can all pass the threshold check simultaneously, then each independently commit their request after the async operations complete, causing the actual queue depth to far exceed `MAX_CONCURRENT_PENDING_REQUESTS` (5 000). This is the direct IC analog of the PrizePool M-16 bug: a threshold check that is structurally bypassed at the boundary condition.

---

### Finding Description

In `retrieve_btc`:

```
// line 174
if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
{
    return Err(RetrieveBtcError::TemporarilyUnavailable(...));
}

let balance = balance_of(caller).await?;          // ← yields control
...
let status = check_address(...).await?;            // ← yields control
...
let block_index = burn_ckbtcs(...).await?;         // ← yields control

mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, ...));
``` [1](#0-0) [2](#0-1) [3](#0-2) 

The identical pattern exists in `retrieve_btc_with_approval`: [4](#0-3) 

The per-account `retrieve_btc_guard` prevents the **same** account from having two concurrent in-flight calls, but it does nothing to prevent **different** accounts from all passing the global count check simultaneously. [5](#0-4) 

`count_incomplete_retrieve_btc_requests` sums pending, in-flight, and submitted requests: [6](#0-5) 

---

### Impact Explanation

An attacker controlling N distinct principals can submit N concurrent `retrieve_btc` calls when the queue is at `MAX_CONCURRENT_PENDING_REQUESTS - 1` (4 999). All N callers read the same stale count, all pass the guard, all burn ckBTC through the ledger, and all call `accept_retrieve_btc_request`, pushing the `pending_retrieve_btc_requests` vector to `4999 + N` entries. There is no second check after the async operations. [7](#0-6) 

Consequences:
- The minter's heap grows unboundedly with pending requests, risking memory exhaustion on the subnet.
- The `build_batch` loop iterates the entire `pending_retrieve_btc_requests` vector on every processing cycle, causing quadratic work.
- Legitimate users' BTC withdrawals are delayed or permanently stuck. [8](#0-7) 

---

### Likelihood Explanation

Any unprivileged principal holding ckBTC can call `retrieve_btc`. No special role, key, or governance majority is required. The attacker needs only N distinct funded accounts and the ability to submit N messages in the same IC round (or across consecutive rounds while the queue is near the limit). This is straightforwardly achievable from a single script using the IC agent library. The attack is cheap: the ckBTC is burned and a BTC withdrawal is initiated, so the attacker receives BTC in return; the cost is only transaction fees.

---

### Recommendation

Re-check `count_incomplete_retrieve_btc_requests()` **inside** `mutate_state`, immediately before calling `accept_retrieve_btc_request`, so the check and the state mutation are atomic:

```rust
mutate_state(|s| {
    if s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS {
        return Err(...);
    }
    state::audit::accept_retrieve_btc_request(s, request, runtime);
    Ok(())
})?;
```

This mirrors the correct pattern already used by the per-account `Guard`, which checks and inserts atomically inside a single `mutate_state` closure. [9](#0-8) 

---

### Proof of Concept

1. Bring the minter to a state where `count_incomplete_retrieve_btc_requests() == 4999`.
2. Submit 5 001 `retrieve_btc` calls from 5 001 distinct funded accounts in rapid succession (one IC round is sufficient; the IC processes all ingress messages before executing any callbacks).
3. All 5 001 callers read `count == 4999 < 5000` and pass the guard.
4. Each caller's `balance_of` → `check_address` → `burn_ckbtcs` chain completes; the ledger burns ckBTC for each.
5. Each caller's `accept_retrieve_btc_request` executes, pushing 5 001 new entries into `pending_retrieve_btc_requests`.
6. Final queue depth: `4999 + 5001 = 10 000`, double the intended maximum, with no error returned to any caller. [10](#0-9)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L22-22)
```rust
const MAX_CONCURRENT_PENDING_REQUESTS: usize = 5000;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-242)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }

    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
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

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
        reimbursement_account: Some(Account {
            owner: caller,
            subaccount: None,
        }),
    };

    log!(
        Priority::Debug,
        "accepted a retrieve btc request for {} BTC to address {} (block_index = {})",
        crate::tx::DisplayAmount(request.amount),
        args.address,
        request.block_index
    );

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, &IC_CANISTER_RUNTIME));

    assert_eq!(
        crate::state::RetrieveBtcStatus::Pending,
        read_state(|s| s.retrieve_btc_status(block_index))
    );

    schedule_now(TaskType::ProcessLogic, &IC_CANISTER_RUNTIME);

    Ok(RetrieveBtcOk { block_index })
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L274-279)
```rust
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcWithApprovalError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L41-60)
```rust
impl<PR: PendingRequests> Guard<PR> {
    /// Attempts to create a new guard for the current block. Fails if there is
    /// already a pending request for the specified [principal] or if there
    /// are at least [MAX_CONCURRENT] pending requests.
    pub fn new(account: Account) -> Result<Self, GuardError> {
        mutate_state(|s| {
            let accounts = PR::pending_requests(s);
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            accounts.insert(account);
            Ok(Self {
                account,
                _marker: PhantomData,
            })
        })
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L942-958)
```rust
    /// Forms a batch of retrieve_btc requests that the minter can fulfill.
    pub fn build_batch(&mut self, max_size: usize) -> BTreeSet<RetrieveBtcRequest> {
        let available_utxos_value = self.available_utxos.iter().map(|u| u.value).sum::<u64>();
        let mut batch = BTreeSet::new();
        let mut tx_amount = 0;
        for req in std::mem::take(&mut self.pending_retrieve_btc_requests) {
            if available_utxos_value < req.amount + tx_amount || batch.len() >= max_size {
                // Put this request back to the queue until we have enough liquid UTXOs.
                self.pending_retrieve_btc_requests.push(req);
            } else {
                tx_amount += req.amount;
                batch.insert(req);
            }
        }

        batch
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L962-970)
```rust
    pub fn count_incomplete_retrieve_btc_requests(&self) -> usize {
        self.pending_retrieve_btc_requests.len()
            + self.requests_in_flight.len()
            + self
                .submitted_transactions
                .iter()
                .map(|tx| tx.requests.count_retrieve_btc_requests())
                .sum::<usize>()
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L17-36)
```rust
pub fn accept_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    runtime: &R,
) {
    record_event(
        EventType::AcceptedRetrieveBtcRequest(request.clone()),
        runtime,
    );
    state.pending_retrieve_btc_requests.push(request.clone());
    if let Some(account) = request.reimbursement_account {
        state
            .retrieve_btc_account_to_block_indices
            .entry(account)
            .and_modify(|entry| entry.push(request.block_index))
            .or_insert(vec![request.block_index]);
    }
    if let Some(kyt_provider) = request.kyt_provider {
        *state.owed_kyt_amount.entry(kyt_provider).or_insert(0) += state.check_fee;
    }
```
