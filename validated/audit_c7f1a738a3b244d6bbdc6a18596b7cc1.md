### Title
Allowance Tracking Inconsistency in `TransactionsAndBalances`: Cumulative Instead of Replacement Semantics - (File: rs/ledger_suite/icrc1/test_utils/src/lib.rs)

### Summary

The `TransactionsAndBalances` helper struct in the ICRC-1 test utilities tracks allowances using **additive/cumulative** semantics when processing `Approve` operations, while the actual on-chain ledger uses **replacement** semantics. This divergence means the test oracle used by the `valid_transactions_strategy` property-based test framework silently tracks incorrect allowance state, causing the `transfer_from_strategy` to generate `transfer_from` transactions that reference allowances larger than what the ledger actually holds — leading to false-positive test passes or incorrect test generation that masks real ledger bugs.

### Finding Description

In `rs/ledger_suite/icrc1/test_utils/src/lib.rs`, the `TransactionsAndBalances::apply` method handles `Operation::Approve` as follows:

```rust
Operation::Approve {
    from,
    spender,
    amount,
    ..
} => {
    self.allowances
        .entry((from, spender))
        .and_modify(|current_allowance| {
            *current_allowance =
                Tokens::from_e8s((*current_allowance).get_e8s() + amount.get_e8s())
        })
        .or_insert(amount);
    self.debit(from, fee);
}
``` [1](#0-0) 

This code **adds** the new approval amount on top of the existing allowance. However, the actual ledger's `AllowanceTable::approve` in `rs/ledger_suite/common/ledger_core/src/approvals.rs` **replaces** the existing allowance with the new amount:

```rust
table.allowances_data.set_allowance(
    key.clone(),
    Allowance {
        amount: amount.clone(),
        expires_at,
        arrived_at: now,
    },
);
``` [2](#0-1) 

This is explicitly tested and documented as non-cumulative behavior in both the ICRC-1 and ICP ledger test suites: [3](#0-2) [4](#0-3) 

The `TransactionsAndBalances` allowance map is used directly by `transfer_from_strategy` to determine which `(from, spender)` pairs have valid allowances and what amounts are available: [5](#0-4) 

Because the oracle inflates allowances cumulatively, the strategy believes larger allowances exist than the ledger actually tracks, and generates `transfer_from` calls that will fail with `InsufficientAllowance` on the real ledger — or, if the test framework catches these failures, silently discards them, masking real bugs.

### Impact Explanation

**Ledger conservation bug / test oracle divergence.** The `valid_transactions_strategy` is used in property-based tests that verify ledger invariants (balance conservation, allowance correctness) across the ICP and ICRC-1 ledger canisters. Because the oracle's allowance state diverges from the ledger's actual state after any second `approve` to the same `(from, spender)` pair, the generated `transfer_from` transactions may:

1. Attempt to spend more than the actual on-chain allowance, causing the ledger to reject them — but the test framework may not catch this as a failure if the strategy simply filters or retries.
2. Cause the oracle's `checked_sub` on the allowance to panic or produce incorrect residual allowance values, masking real ledger bugs in allowance accounting.
3. Allow the `expected_allowance` field in subsequent `approve` calls to be set to the inflated (wrong) value, causing the ledger to reject the approve with `AllowanceChanged` — again silently masking the discrepancy.

An unprivileged ingress sender who studies the test strategy can predict that the test harness will not catch certain allowance-related bugs, reducing assurance in the ledger's correctness guarantees.

### Likelihood Explanation

**High.** Any property-based test run that generates two or more `approve` operations for the same `(from, spender)` pair will trigger this divergence. Given the strategy generates sequences of up to hundreds of transactions with `approve` weighted at 10x relative to burns and mints, this scenario occurs in virtually every non-trivial test run. [6](#0-5) 

### Recommendation

Change the `Approve` arm in `TransactionsAndBalances::apply` to use replacement semantics, matching the actual ledger behavior:

```rust
Operation::Approve {
    from,
    spender,
    amount,
    ..
} => {
    assert_eq!(tx.from(), from);
    // Replace (not add) the allowance, matching ledger replacement semantics
    if amount.get_e8s() == 0 {
        self.allowances.remove(&(from, spender));
    } else {
        self.allowances.insert((from, spender), amount);
    }
    self.debit(from, fee);
}
```

Also ensure that `expected_allowance` is validated against the oracle's current allowance before inserting, consistent with how `InMemoryLedger::set_allowance` does it in `rs/ledger_suite/test_utils/in_memory_ledger/src/lib.rs`. [7](#0-6) 

### Proof of Concept

1. The `valid_transactions_strategy` generates a sequence including:
   - `approve(from=A, spender=B, amount=100_000)` → oracle records `allowances[(A,B)] = 100_000`; ledger records `100_000`.
   - `approve(from=A, spender=B, amount=50_000)` → oracle records `allowances[(A,B)] = 150_000` (cumulative); ledger records `50_000` (replacement).
2. `transfer_from_strategy` sees oracle allowance of `150_000` and generates `transfer_from(from=A, spender=B, amount=140_000)`.
3. The ledger rejects with `InsufficientAllowance { allowance: 50_000 }`.
4. The oracle's `checked_sub` panics or the test silently discards the transaction, hiding the divergence.
5. Any subsequent `approve` with `expected_allowance=Some(150_000)` (from the oracle's inflated view) is rejected by the ledger with `AllowanceChanged { current_allowance: 50_000 }`, again silently masked.

The root cause is exclusively in `rs/ledger_suite/icrc1/test_utils/src/lib.rs` lines 657–663, where `and_modify` adds rather than replaces the allowance amount. [8](#0-7)

### Citations

**File:** rs/ledger_suite/icrc1/test_utils/src/lib.rs (L650-664)
```rust
            Operation::Approve {
                from,
                spender,
                amount,
                ..
            } => {
                assert_eq!(tx.from(), from);
                self.allowances
                    .entry((from, spender))
                    .and_modify(|current_allowance| {
                        *current_allowance =
                            Tokens::from_e8s((*current_allowance).get_e8s() + amount.get_e8s())
                    })
                    .or_insert(amount);
                self.debit(from, fee);
```

**File:** rs/ledger_suite/icrc1/test_utils/src/lib.rs (L1216-1226)
```rust
                let allowances_for_from: Vec<(Account, Tokens)> = allowance_map
                    .range((
                        Included((from, MIN_ACCOUNT)),
                        Included((from, MAX_ACCOUNT)),
                    ))
                    .map(|((allowance_from, spender), allowance)| {
                        // Ensure the from account in the allowance matches the selected from account
                        assert_eq!(&from, allowance_from);
                        (*spender, *allowance)
                    })
                    .collect();
```

**File:** rs/ledger_suite/icrc1/test_utils/src/lib.rs (L1405-1418)
```rust
            let mut options = vec![];

            if !excluded_transaction_types.contains(&TransactionTypes::Approve) {
                options.push((10, approve_strategy));
            }
            if !excluded_transaction_types.contains(&TransactionTypes::Burn) {
                options.push((1, burn_strategy));
            }
            if !excluded_transaction_types.contains(&TransactionTypes::Mint) {
                options.push((1, mint_strategy));
            }
            if !excluded_transaction_types.contains(&TransactionTypes::Transfer) {
                options.push((1000, transfer_strategy));
            }
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L300-307)
```rust
                    table.allowances_data.set_allowance(
                        key.clone(),
                        Allowance {
                            amount: amount.clone(),
                            expires_at,
                            arrived_at: now,
                        },
                    );
```

**File:** rs/ledger_suite/icrc1/ledger/src/tests.rs (L91-157)
```rust
#[test]
fn test_approvals_are_not_cumulative() {
    let now = ts(12345678);

    let mut ctx = Ledger::from_init_args(DummyLogger, default_init_args(), now);

    let from = test_account_id(1);
    let spender = test_account_id(2);

    ctx.balances_mut().mint(&from, tokens(100_000)).unwrap();

    let approved_amount = tokens(150_000);
    let fee = tokens(10_000);

    let tr = Transaction {
        operation: Operation::Approve {
            from,
            spender,
            amount: approved_amount,
            expected_allowance: None,
            expires_at: None,
            fee: Some(fee),
        },
        created_at_time: None,
        memo: None,
    };
    tr.apply(&mut ctx, now, Tokens::ZERO).unwrap();

    assert_eq!(ctx.balances().account_balance(&from), tokens(90_000));
    assert_eq!(ctx.balances().account_balance(&spender), tokens(0));

    assert_eq!(
        ctx.approvals().allowance(&from, &spender, now),
        Allowance {
            amount: approved_amount,
            expires_at: None,
            arrived_at: ts(0),
        },
    );

    let new_allowance = tokens(200_000);

    let expiration = now + Duration::from_secs(300);
    let tr = Transaction {
        operation: Operation::Approve {
            from,
            spender,
            amount: new_allowance,
            expected_allowance: None,
            expires_at: Some(expiration.as_nanos_since_unix_epoch()),
            fee: Some(fee),
        },
        created_at_time: None,
        memo: None,
    };
    tr.apply(&mut ctx, now, Tokens::ZERO).unwrap();

    assert_eq!(ctx.balances().account_balance(&from), tokens(80_000));
    assert_eq!(ctx.balances().account_balance(&spender), tokens(0));
    assert_eq!(
        ctx.approvals().allowance(&from, &spender, now),
        Allowance {
            amount: new_allowance,
            expires_at: Some(expiration),
            arrived_at: ts(0),
        }
    );
```

**File:** rs/ledger_suite/icp/ledger/src/tests.rs (L795-860)
```rust
#[test]
fn test_approvals_are_not_cumulative() {
    let mut ctx = Ledger::default();

    let from = test_account_id(1);
    let spender = test_account_id(2);
    let now = ts(12345678);

    ctx.balances_mut().mint(&from, tokens(100_000)).unwrap();

    let approved_amount = tokens(150_000);
    let fee = tokens(10_000);

    apply_operation(
        &mut ctx,
        &Operation::Approve {
            from,
            spender,
            allowance: approved_amount,
            expected_allowance: None,
            expires_at: None,
            fee,
        },
        now,
    )
    .unwrap();

    assert_eq!(ctx.balances().account_balance(&from), tokens(90_000));
    assert_eq!(ctx.balances().account_balance(&spender), tokens(0));

    assert_eq!(
        ctx.approvals().allowance(&from, &spender, now),
        Allowance {
            amount: approved_amount,
            expires_at: None,
            arrived_at: TimeStamp::from_nanos_since_unix_epoch(0),
        },
    );

    let new_allowance = tokens(200_000);

    let expiration = now + Duration::from_secs(300);
    apply_operation(
        &mut ctx,
        &Operation::Approve {
            from,
            spender,
            allowance: new_allowance,
            expected_allowance: None,
            expires_at: Some(expiration),
            fee,
        },
        now,
    )
    .unwrap();

    assert_eq!(ctx.balances().account_balance(&from), tokens(80_000));
    assert_eq!(ctx.balances().account_balance(&spender), tokens(0));
    assert_eq!(
        ctx.approvals().allowance(&from, &spender, now),
        Allowance {
            amount: new_allowance,
            expires_at: Some(expiration),
            arrived_at: TimeStamp::from_nanos_since_unix_epoch(0),
        }
    );
```

**File:** rs/ledger_suite/test_utils/in_memory_ledger/src/lib.rs (L378-430)
```rust
    fn set_allowance(
        &mut self,
        from: &AccountId,
        spender: &AccountId,
        amount: &Tokens,
        expected_allowance: &Option<Tokens>,
        expires_at: &Option<u64>,
        arrived_at: TimeStamp,
    ) {
        let key = ApprovalKey::from((from, spender));
        if let Some(expected_allowance) = expected_allowance {
            match self.allowances.get(&key) {
                None => {
                    // No in-memory allowance, so the expected allowance should be zero
                    assert!(
                        expected_allowance.is_zero(),
                        "Expected allowance of ({expected_allowance:?}) for key {key:?} does not match in-memory allowance (None, interpreted as 0)"
                    );
                }
                Some(in_memory_allowance) => {
                    // An in-memory allowance is set
                    let in_memory_allowance_amount = match &in_memory_allowance.expires_at {
                        // If the in-memory allowance has no expiration, use the amount as-is
                        None => &in_memory_allowance.amount.clone(),
                        Some(expires_at) => {
                            &if expires_at >= &arrived_at {
                                // If the in-memory allowance has not expired, use the amount as-is
                                in_memory_allowance.amount.clone()
                            } else {
                                // If the in-memory allowance has expired, interpret as zero
                                Tokens::zero()
                            }
                        }
                    };
                    assert_eq!(
                        in_memory_allowance_amount, expected_allowance,
                        "Expected allowance of ({expected_allowance:?}) for key {key:?} does not match in-memory allowance ({in_memory_allowance_amount:?})"
                    );
                }
            }
        }
        if amount == &Tokens::zero() {
            self.allowances.remove(&key);
        } else {
            self.allowances.insert(
                key,
                Allowance {
                    amount: amount.clone(),
                    expires_at: expires_at.map(TimeStamp::from_nanos_since_unix_epoch),
                    arrived_at,
                },
            );
        }
```
