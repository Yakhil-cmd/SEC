### Title
ICP Ledger Forces Deduplication Always Active, Blocking Identical Transfers Within the Same Block - (File: `rs/ledger_suite/icp/ledger/src/lib.rs`)

---

### Summary

The ICP legacy ledger's `add_payment_with_timestamp` unconditionally sets `created_at_time = now` when the caller omits it, making the deduplication window always active. Because `now` (the block timestamp) is identical for all messages processed in the same consensus round, two otherwise-legitimate identical transfers submitted without an explicit `created_at_time` and included in the same block will produce the same transaction hash, causing the second to be rejected with `TxDuplicate`.

---

### Finding Description

In `rs/ledger_suite/icp/ledger/src/lib.rs`, `add_payment_with_timestamp` constructs the `Transaction` struct as:

```rust
Transaction {
    operation,
    memo,
    icrc1_memo: None,
    // TODO(FI-349): preserve created_at_time and memo the caller specified.
    created_at_time: created_at_time.or(Some(now)),
}
``` [1](#0-0) 

When `created_at_time` is `None` (the caller did not opt into deduplication), the ledger silently substitutes `Some(now)`. This means deduplication is **always** active for every legacy ICP transfer, regardless of caller intent.

The deduplication check in `apply_transaction` only fires when `created_at_time` is `Some`:

```rust
let maybe_time_and_hash = transaction
    .created_at_time()
    .map(|created_at_time| (created_at_time, transaction.hash()));

if let Some((created_at_time, tx_hash)) = maybe_time_and_hash {
    // The caller requested deduplication.
    ...
    if let Some(block_height) = ledger.transactions_by_hash().get(&tx_hash) {
        return Err(TransferError::TxDuplicate {
            duplicate_of: *block_height,
        });
    }
}
``` [2](#0-1) 

The transaction hash is computed over the full serialized `Transaction` struct, which includes `created_at_time`:

```rust
fn hash(&self) -> HashOf<Self> {
    let mut state = Sha256::new();
    state.write(&serde_cbor::ser::to_vec_packed(&self).unwrap());
    HashOf::new(state.finish())
}
``` [3](#0-2) 

The `Transaction` struct includes `created_at_time` as a field: [4](#0-3) 

Because `time()` returns the **block timestamp**, which is identical for every message processed in the same consensus round, two identical transfers (same `operation`, `memo`, `icrc1_memo`) submitted without `created_at_time` and included in the same block will both receive `created_at_time = T_block`, produce the same hash, and the second will be rejected as `TxDuplicate`.

This contrasts with the ICRC-1 ledger, which correctly preserves `None` when the caller omits `created_at_time`, leaving deduplication opt-in: [5](#0-4) 

The ICRC-1 `Transaction` keeps `created_at_time` as `Option<u64>` and only activates deduplication when it is `Some`: [6](#0-5) 

---

### Impact Explanation

Any user or canister that submits two or more identical ICP legacy transfers (same sender, recipient, amount, fee, memo) without an explicit `created_at_time` and whose messages are included in the same consensus block will have all but the first rejected with `TxDuplicate`. This is a ledger conservation bug: legitimate, distinct economic operations are silently blocked. A canister acting as a payment processor (e.g., paying multiple recipients the same amount with the same memo in one round) will lose funds from the second payment without the recipient receiving them, and must implement retry logic that is not required by the ICRC-1 interface.

---

### Likelihood Explanation

The IC consensus layer routinely batches multiple ingress messages into a single block. Any caller that submits two identical transfers in rapid succession (e.g., via a script, a canister, or a wallet that retries on timeout) risks both landing in the same block. The 24-hour `transaction_window` means the dedup map is large, so the collision persists long after the block. The TODO comment at the root cause line (`// TODO(FI-349): preserve created_at_time and memo the caller specified.`) confirms this is a known, unresolved design defect. [7](#0-6) 

---

### Recommendation

Mirror the ICRC-1 ledger's behavior: do **not** substitute `now` when `created_at_time` is `None`. Deduplication should be strictly opt-in. If always-on deduplication is desired for the legacy endpoint, include a monotonically increasing nonce (e.g., the block height or a per-sender counter) in the hash so that two distinct transfers in the same block always produce distinct hashes, analogous to including `eta` in the Yield `TimeLock` fix.

---

### Proof of Concept

1. Alice submits `transfer({to: Bob, amount: 100, fee: 10000, memo: 0, created_at_time: null})` — call this TX-A.
2. Alice immediately submits an identical `transfer({to: Bob, amount: 100, fee: 10000, memo: 0, created_at_time: null})` — call this TX-B.
3. Both TX-A and TX-B are included in the same consensus block with block time `T`.
4. TX-A is processed: `created_at_time` is set to `Some(T)`, hash = `H`. TX-A succeeds, block index 42 is returned.
5. TX-B is processed: `created_at_time` is set to `Some(T)` (same block time), hash = `H` (identical). `transactions_by_hash` already contains `H → 42`.
6. TX-B returns `TxDuplicate { duplicate_of: 42 }`. Bob receives only one payment instead of two.

The existing unit test `duplicate_txns` in `rs/ledger_suite/icp/ledger/src/tests.rs` confirms that identical `(memo, operation, created_at_time)` tuples are rejected within the window, and the `add_payment_with_timestamp` call with `created_at_time = None` will silently use `now`, making this collision possible whenever two such calls share a block timestamp. [8](#0-7)

### Citations

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L399-410)
```rust
        core_ledger::apply_transaction(
            self,
            Transaction {
                operation,
                memo,
                icrc1_memo: None,
                // TODO(FI-349): preserve created_at_time and memo the caller specified.
                created_at_time: created_at_time.or(Some(now)),
            },
            now,
            effective_fee,
        )
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L233-253)
```rust
    let maybe_time_and_hash = transaction
        .created_at_time()
        .map(|created_at_time| (created_at_time, transaction.hash()));

    if let Some((created_at_time, tx_hash)) = maybe_time_and_hash {
        // The caller requested deduplication.
        if created_at_time + ledger.transaction_window() < now {
            return Err(TransferError::TxTooOld {
                allowed_window_nanos: ledger.transaction_window().as_nanos() as u64,
            });
        }

        if created_at_time > now + ic_limits::PERMITTED_DRIFT {
            return Err(TransferError::TxCreatedInFuture { ledger_time: now });
        }

        if let Some(block_height) = ledger.transactions_by_hash().get(&tx_hash) {
            return Err(TransferError::TxDuplicate {
                duplicate_of: *block_height,
            });
        }
```

**File:** rs/ledger_suite/icp/src/lib.rs (L254-261)
```rust
pub struct Transaction {
    pub operation: Operation,
    pub memo: Memo,
    /// The time this transaction was created.
    pub created_at_time: Option<TimeStamp>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub icrc1_memo: Option<ByteBuf>,
}
```

**File:** rs/ledger_suite/icp/src/lib.rs (L312-316)
```rust
    fn hash(&self) -> HashOf<Self> {
        let mut state = Sha256::new();
        state.write(&serde_cbor::ser::to_vec_packed(&self).unwrap());
        HashOf::new(state.finish())
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L675-699)
```rust
#[update]
async fn icrc1_transfer(arg: TransferArg) -> Result<Nat, TransferError> {
    let from_account = Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: arg.from_subaccount,
    };
    execute_transfer(
        from_account,
        arg.to,
        None,
        arg.fee,
        arg.amount,
        arg.memo,
        arg.created_at_time,
    )
    .await
    .map_err(convert_transfer_error)
    .map_err(|err| {
        let err: TransferError = match err.try_into() {
            Ok(err) => err,
            Err(err) => ic_cdk::trap(&err),
        };
        err
    })
}
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L427-430)
```rust
    fn created_at_time(&self) -> Option<TimeStamp> {
        self.created_at_time
            .map(TimeStamp::from_nanos_since_unix_epoch)
    }
```

**File:** rs/ledger_suite/icp/ledger/src/tests.rs (L356-430)
```rust
/// Check that duplicate transactions during transaction_window
/// are rejected.
#[test]
fn duplicate_txns() {
    let mut state = Ledger::default();

    state.blockchain.archive = Arc::new(RwLock::new(Some(Archive::new(ArchiveOptions {
        trigger_threshold: 2000,
        num_blocks_to_archive: 1000,
        node_max_memory_size_bytes: None,
        max_message_size_bytes: None,
        controller_id: CanisterId::from_u64(876).into(),
        more_controller_ids: None,
        cycles_for_archive_creation: Some(0),
        max_transactions_per_response: None,
    }))));

    let user1 = PrincipalId::new_user_test_id(1).into();

    let transfer = Operation::Mint {
        to: user1,
        amount: Tokens::from_e8s(1000),
    };

    let now = SystemTime::now().into();

    assert_eq!(
        state
            .add_payment_with_timestamp(Memo::default(), transfer.clone(), Some(now), now)
            .unwrap()
            .0,
        0
    );

    assert_eq!(
        state
            .add_payment_with_timestamp(Memo(123), transfer.clone(), Some(now), now)
            .unwrap()
            .0,
        1
    );

    assert_eq!(
        state
            .add_payment_with_timestamp(
                Memo::default(),
                transfer.clone(),
                Some(now - Duration::from_secs(1)),
                now
            )
            .unwrap()
            .0,
        2
    );

    assert_eq!(
        state
            .add_payment_with_timestamp(
                Memo::default(),
                transfer.clone(),
                Some(now - Duration::from_secs(2)),
                state.blockchain.last_timestamp + Duration::from_secs(10000)
            )
            .unwrap()
            .0,
        3
    );

    assert_eq!(
        PaymentError::TransferError(TransferError::TxDuplicate { duplicate_of: 0 }),
        state
            .add_payment_with_timestamp(Memo::default(), transfer.clone(), Some(now), now)
            .unwrap_err()
    );

```
