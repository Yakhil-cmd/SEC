### Title
Spender Account Registered in `list_subaccounts` Without Holding Any Tokens - (File: rs/ledger_suite/icrc1/index-ng/src/main.rs)

### Summary
In the ICRC-1 index-ng canister, processing an `Approve` block unconditionally inserts the spender's account into the `AccountDataMap` with a balance of zero, even though the spender has received no tokens. Because `list_subaccounts` returns every entry in that map without filtering on balance, any unprivileged caller can inject arbitrary subaccounts into any principal's subaccount list by paying only the ledger approve fee.

### Finding Description
`process_balance_changes` in `rs/ledger_suite/icrc1/index-ng/src/main.rs` dispatches on the block operation type. For `Operation::Approve`, after debiting the fee from the approver, it executes:

```rust
// If the account is new, this will add it to the AccountDataMap with balance 0
// and thus show up in a `list_subaccount` query.
change_balance(spender, |balance| balance);   // line 1114
``` [1](#0-0) 

`change_balance` with the identity closure `|balance| balance` creates a zero-balance entry in the stable `AccountDataMap` (keyed by `AccountDataType::Balance`) if the spender did not previously exist there.

`list_subaccounts` then iterates over that map for the requested principal and returns every subaccount it finds, with no balance filter:

```rust
with_account_data(|data| {
    data.range(range)
        .take(DEFAULT_MAX_BLOCKS_PER_RESPONSE as usize)
        .map(|((_, (_, subaccount)), _)| subaccount)
        .collect()
})
``` [2](#0-1) 

The behaviour is confirmed by the production test:

> "The subaccount 1 should show up in a `list_subaccount` query although it has only been involved in an Approve transaction" [3](#0-2) 

The `AccountDataMap` is stored in stable memory under `ACCOUNT_DATA_MEMORY_ID`: [4](#0-3) 

### Impact Explanation
An unprivileged ingress sender (Alice, principal A) can call `icrc2_approve` on any ICRC-1 ledger whose index-ng is deployed, naming an arbitrary victim account (principal B, subaccount S) as the spender. After the index-ng syncs that block, `list_subaccounts({ owner: B })` returns subaccount S with an on-ledger balance of zero. Repeating this with many distinct subaccounts inflates the victim's subaccount list with phantom entries. Wallets, portfolio trackers, and DeFi front-ends that rely on `list_subaccounts` to enumerate a user's holdings will display accounts the user never controlled or funded. The impact is state-list pollution analogous to the original report: the account list contains entries the account does not actually hold, with no effect on actual token balances or collateral calculations.

### Likelihood Explanation
The attack requires only a standard `icrc2_approve` call on any ICRC-1 ledger that has an index-ng canister. No privileged access, governance majority, or key material is needed. The cost to the attacker is one approve fee per injected subaccount. The ICRC-2 approve endpoint is a publicly reachable ingress update call available on all production ICRC-1 ledgers (ICP ledger, ckBTC ledger, ckETH ledger, SNS token ledgers, etc.).

### Recommendation
Filter `list_subaccounts` to exclude entries whose stored balance is zero, or introduce a separate `AccountDataType` variant for "spender-only" accounts so they are not mixed into the balance-keyed range used by `list_subaccounts`. Alternatively, document explicitly that `list_subaccounts` may return zero-balance accounts that were named as spenders, so downstream consumers can apply their own balance filter.

### Proof of Concept
1. Alice calls `icrc2_approve` on an ICRC-1 ledger, setting `spender = { owner: B, subaccount: S }` for any victim principal B and subaccount S she chooses.
2. The ledger appends an `Approve` block.
3. The index-ng syncs the block; `process_balance_changes` reaches the `Operation::Approve` arm and executes `change_balance(spender, |balance| balance)` (line 1114), inserting `(B, S) → 0` into `ACCOUNT_DATA`.
4. Alice calls `list_subaccounts({ owner: B })` on the index-ng; the response includes subaccount S even though `icrc1_balance_of({ owner: B, subaccount: S })` returns 0 on the ledger.
5. Repeating step 1 with different subaccounts S₁…Sₙ (up to `DEFAULT_MAX_BLOCKS_PER_RESPONSE` per page) fills B's subaccount list with phantom entries, each costing Alice one approve fee. [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L78-79)
```rust
type AccountDataMapKey = (AccountDataType, (Blob<29>, [u8; 32]));
type AccountDataMap = StableBTreeMap<AccountDataMapKey, Tokens, VM>;
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1087-1121)
```rust
            Operation::Approve {
                from, fee, spender, ..
            } => {
                let fee = match fee.or(block.effective_fee) {
                    Some(fee) => fee,
                    // NB. There was a bug in the ledger which would create
                    // approve blocks with the fee fields unset. The bug was
                    // quickly fixed, but there are a few blocks on the mainnet
                    // that don't have their fee fields populated.
                    None => match with_state(|state| state.last_fee) {
                        Some(last_fee) => {
                            log!(
                                P1,
                                "fee and effective_fee aren't set in block {block_index}, using last transfer fee {last_fee}"
                            );
                            last_fee
                        }
                        None => ic_cdk::trap(format!(
                            "bug: index is stuck because block with index {block_index} doesn't contain a fee and no fee has been recorded before"
                        )),
                    },
                };

                // It is possible that the spender account has not existed prior to this approve transaction.
                // Until a transfer_from transaction occurs such account would not show up in a `list_subaccounts` query as the spender is not involved in any credit or debit calls at this point.
                // To ensure that the account still shows up in the `list_subaccount` query we can simply call `change_balance` without actually changing the balance.
                // If the account is new, this will add it to the AccountDataMap with balance 0 and thus show up in a `list_subaccount` query.
                change_balance(spender, |balance| balance);

                debit(block_index, from, fee);

                if let Some(fee_collector_107) = get_fee_collector_107().flatten() {
                    credit(block_index, fee_collector_107, fee);
                }
            }
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1352-1375)
```rust
fn list_subaccounts(args: ListSubaccountsArgs) -> Vec<Subaccount> {
    let start_key = balance_key(Account {
        owner: args.owner,
        subaccount: args.start,
    });
    let end_key = balance_key(Account {
        owner: args.owner,
        subaccount: Some([u8::MAX; 32]),
    });
    let range = (
        if args.start.is_none() {
            Included(start_key)
        } else {
            Excluded(start_key)
        },
        Included(end_key),
    );
    with_account_data(|data| {
        data.range(range)
            .take(DEFAULT_MAX_BLOCKS_PER_RESPONSE as usize)
            .map(|((_, (_, subaccount)), _)| subaccount)
            .collect()
    })
}
```

**File:** rs/ledger_suite/icrc1/index-ng/tests/tests.rs (L1748-1760)
```rust
    // Make an approve transaction with the spender being a completly new account
    approve(env, ledger_id, account(1, 0), account(2, 1), 100);
    wait_until_sync_is_completed(env, index_id, ledger_id);

    // The balance of the new account should be 0. Approve transactions do not change the balance of the spender
    assert_eq!(icrc1_balance_of(env, ledger_id, account(2, 1)), 0);

    let subaccounts = list_subaccounts(env, index_id, PrincipalId(account(2, 0).owner), None);

    // There should exist one subaccount for the principal of account (2,0)
    assert_eq!(subaccounts.len(), 1);
    // The subaccount 1 should show up in a `list_subaccount` query although it has only been involved in an Approve transaction
    assert!(subaccounts.contains(&account(2, 1).subaccount.unwrap()));
```
