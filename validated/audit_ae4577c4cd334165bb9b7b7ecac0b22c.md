### Title
ICRC-1 Ledger `from_init_args` Accepts Duplicate Accounts in `initial_balances`, Inflating `icrc1_total_supply` - (File: rs/ledger_suite/icrc1/ledger/src/lib.rs)

---

### Summary
The ICRC-1 ledger's initialization function does not validate for duplicate accounts in the `initial_balances` vector. When the same account appears twice with different token amounts, both mint transactions succeed independently, inflating the on-chain total supply beyond the intended genesis allocation. This is a direct analog to the ynLSD duplicate-asset initialization bug.

---

### Finding Description

The `InitArgs.initial_balances` field is declared as a Candid `vec record { Account; nat }` — a plain ordered list, not a map — meaning duplicate `Account` entries are structurally permitted by the interface. [1](#0-0) 

In `Ledger::from_init_args`, the code iterates over every entry and calls `apply_transaction` with a fresh mint for each one, with no prior deduplication check: [2](#0-1) 

The `apply_transaction` function only deduplicates by **transaction hash**. Because each mint is constructed as `Transaction::mint(account, amount, Some(now), None)`, the hash covers `(account, amount, created_at_time, memo)`. Two entries for the same account with **different amounts** produce different hashes and therefore both pass the deduplication gate: [3](#0-2) 

The `credit` function in `Balances` correctly accumulates the balance for the account, but the `token_pool` is debited twice, so `total_supply()` — which is `Tokens::max_value() - token_pool` — is inflated by the second mint amount: [4](#0-3) [5](#0-4) 

The public `icrc1_total_supply` query then returns this inflated value: [6](#0-5) 

Note: exact duplicates (same account **and** same amount) produce the same hash and are caught by `apply_transaction`, causing a `panic!` that aborts the install. The gap is specifically **same account, different amounts**, which silently succeeds.

---

### Impact Explanation

Any ICRC-1 ledger deployed with duplicate accounts (different amounts) in `initial_balances` will report a `total_supply` larger than the sum of intended genesis allocations. Downstream systems that rely on `icrc1_total_supply` for share/ratio calculations (e.g., vault-style canisters, DEX liquidity pools, index canisters) will compute incorrect exchange rates or balances. Token holders whose accounts were duplicated receive a larger-than-intended balance, effectively minting tokens out of thin air at genesis.

---

### Likelihood Explanation

The `initial_balances` field is a `Vec`, not a `BTreeMap`, so the type system offers no protection. The `InitArgsBuilder::with_initial_balance` helper simply pushes to the vector without checking for existing entries: [7](#0-6) 

A canister developer composing `initial_balances` programmatically (e.g., merging neuron accounts with direct balances, or iterating over two separate lists) can easily produce duplicates. The SNS test utility already demonstrates this pattern — it pushes neuron accounts into the ledger's `initial_balances` Vec without a uniqueness check: [8](#0-7) 

Production paths (SNS, ckETH/ckERC20 orchestrator) happen to be safe because SNS uses a `BTreeMap` to build accounts before passing them to the ledger, and the orchestrator always passes `initial_balances: vec![]`. However, the ledger itself provides no safety net for any other deployment.

---

### Recommendation

In `Ledger::from_init_args`, before processing `initial_balances`, collect all accounts into a `BTreeSet` and trap if any duplicate is detected:

```rust
let mut seen = BTreeSet::new();
for (account, _) in &initial_balances {
    if !seen.insert(account.clone()) {
        ic_cdk::trap(format!(
            "Duplicate account in initial_balances: {:?}", account
        ));
    }
}
```

Alternatively, aggregate balances into a `BTreeMap<Account, Nat>` before minting, so duplicate entries are merged rather than double-minted.

---

### Proof of Concept

```
// Deploy an ICRC-1 ledger with:
InitArgs {
    minting_account: minter,
    initial_balances: vec![
        (alice, Nat::from(1_000_000_u64)),   // first entry
        (alice, Nat::from(2_000_000_u64)),   // duplicate account, different amount
    ],
    ...
}

// After install:
// alice.balance  == 3_000_000   (both mints credited)
// icrc1_total_supply() == 3_000_000  (inflated; intended was 2_000_000)
// The second mint has a different tx hash (different amount), so
// apply_transaction's dedup check does NOT fire.
``` [2](#0-1) [9](#0-8)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L100-122)
```text
type InitArgs = record {
  minting_account : Account;
  fee_collector_account : opt Account;
  transfer_fee : nat;
  decimals : opt nat8;
  max_memo_length : opt nat16;
  token_symbol : text;
  token_name : text;
  metadata : vec record { text; MetadataValue };
  initial_balances : vec record { Account; nat };
  feature_flags : opt FeatureFlags;
  archive_options : record {
    num_blocks_to_archive : nat64;
    max_transactions_per_response : opt nat64;
    trigger_threshold : nat64;
    max_message_size_bytes : opt nat64;
    cycles_for_archive_creation : opt nat64;
    node_max_memory_size_bytes : opt nat64;
    controller_id : principal;
    more_controller_ids : opt vec principal
  };
  index_principal : opt principal
};
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L731-739)
```rust
        for (account, balance) in initial_balances.into_iter() {
            let amount = Tokens::try_from(balance.clone()).unwrap_or_else(|e| {
                panic!("failed to convert initial balance {balance} to tokens: {e}")
            });
            let mint = Transaction::mint(account, amount, Some(now), None);
            apply_transaction(&mut ledger, mint, now, Tokens::ZERO).unwrap_or_else(|err| {
                panic!("failed to mint {balance} tokens to {account}: {err:?}")
            });
        }
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

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L145-156)
```rust
    pub fn mint(
        &mut self,
        to: &S::AccountId,
        amount: S::Tokens,
    ) -> Result<(), BalanceError<S::Tokens>> {
        self.token_pool = self
            .token_pool
            .checked_sub(&amount)
            .expect("total token supply exceeded");
        self.credit(to, amount);
        Ok(())
    }
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L212-217)
```rust
    pub fn total_supply(&self) -> S::Tokens {
        S::Tokens::max_value().checked_sub(&self.token_pool).expect(
            "It is expected that the token_pool is always smaller than \
            or equal to Tokens::max_value(), yet subtracting it lead to underflow",
        )
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L538-541)
```rust
#[query(name = "icrc1_total_supply")]
fn icrc1_total_supply() -> Nat {
    Access::with_ledger(|ledger| ledger.balances().total_supply().into())
}
```

**File:** rs/sns/test_utils/src/itest_helpers.rs (L445-448)
```rust
                ledger
                    .initial_balances
                    .push((aid, n.cached_neuron_stake_e8s.into()));
            }
```
