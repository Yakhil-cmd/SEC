### Title
`last_fee` Not Updated for `Approve` Operations Causes Stale Fee Fallback and Incorrect Balance Accounting in ICRC-1 Index-NG - (File: rs/ledger_suite/icrc1/index-ng/src/main.rs)

### Summary
The `last_fee` state variable in the ICRC-1 index-ng canister is updated by `Burn`, `Mint`, and `Transfer` operations but is **never updated** when processing `Approve` operations, even when the `Approve` block carries a valid fee. This is structurally identical to the RibbonThetaVault bug: a "last recorded amount" variable is conditionally skipped for a class of events, causing it to go stale. When a legacy `Approve` block with no fee field is encountered (a known historical ledger defect acknowledged in the code), the index falls back to the stale `last_fee`. If the ledger fee changed between the last Transfer/Burn/Mint and those legacy blocks, the wrong fee is debited from the `from` account, permanently corrupting the index canister's balance state for affected accounts.

### Finding Description
In `process_balance_changes` at line 1021, `last_fee` is updated in three branches:

- **Burn** (line 1037): `mutate_state(|s| s.last_fee = Some(fee))` — only when `effective_fee` is `Some`
- **Mint** (line 1053): `mutate_state(|s| s.last_fee = Some(fee))` — only when `effective_fee` is `Some`
- **Transfer** (line 1072): `mutate_state(|s| s.last_fee = Some(fee))` — always (traps if absent)

The `Approve` branch (lines 1087–1121) **never calls `mutate_state` to update `last_fee`**, even when `fee.or(block.effective_fee)` resolves to `Some(fee)` at line 1090–1091. The resolved fee is used locally to `debit` the `from` account (line 1116), but `last_fee` is left unchanged.

The fallback at lines 1096–1107 reads:
```rust
None => match with_state(|state| state.last_fee) {
    Some(last_fee) => { ... last_fee }
    None => ic_cdk::trap(...)
}
```
This fallback is reached for legacy mainnet `Approve` blocks that have neither `fee` nor `effective_fee` set — a defect the code itself acknowledges at lines 1092–1095. The value returned is whatever `last_fee` was set to by the most recent Transfer/Burn/Mint, which may be arbitrarily old and from a different fee epoch.

The `last_fee` field is declared in `State` at line 135 and defaults to `None` at line 172.

### Impact Explanation
The index canister's per-account balance map is the authoritative source queried by wallets, DeFi integrations, and other canisters via `get_account_transactions` and `get_blocks`. When `last_fee` is stale:

- If the actual fee at the time of the legacy block was **higher** than `last_fee`, the index under-debits the `from` account → the index reports a balance **higher** than the true ledger balance.
- If the actual fee was **lower** than `last_fee`, the index over-debits → the index reports a balance **lower** than the true ledger balance, potentially causing the `debit` call at line 1116 to trap with an underflow (line 1141–1143), **halting block ingestion entirely** for that index instance.

The second scenario — a trap in `debit` — is the more severe outcome: the index canister becomes permanently stuck and cannot process any further blocks, making all balance and transaction history queries stale for all accounts, not just the affected one.

### Likelihood Explanation
The legacy fee-less `Approve` blocks exist on mainnet and are fixed in the chain. The ICP ledger fee has been changed via NNS governance proposals historically. Any index canister that:
1. Processes a chain segment where the fee changed between the last Transfer and a legacy fee-less `Approve` block, **and**
2. Has not yet processed those blocks (e.g., a freshly deployed or re-initialized index)

will exhibit this bug. The entry path requires no privileged access: any unprivileged user can deploy an ICRC-1 index-ng canister pointed at the affected ledger, or query an existing one that is stuck.

### Recommendation
In the `Approve` branch of `process_balance_changes`, update `last_fee` whenever the resolved fee is `Some`:

```rust
Operation::Approve { from, fee, spender, .. } => {
    let fee = match fee.or(block.effective_fee) {
        Some(fee) => {
            mutate_state(|s| s.last_fee = Some(fee)); // ADD THIS
            fee
        }
        None => match with_state(|state| state.last_fee) { ... }
    };
    ...
}
```

This mirrors the pattern used in the `Burn` and `Mint` branches and ensures `last_fee` always reflects the most recently observed fee across all fee-bearing operation types.

### Proof of Concept

**Sequence that corrupts the index balance state:**

1. Block N: `Transfer` at fee = 10_000 e8s → `last_fee = 10_000`
2. NNS governance proposal changes ledger fee to 100_000 e8s
3. Block N+1: `Approve` with `fee = Some(100_000)` → index debits 100_000 from `from` correctly, but **`last_fee` remains 10_000**
4. Block N+2: legacy `Approve` with `fee = None` and `effective_fee = None` (historical bug block) → index falls back to `last_fee = 10_000`, debits only 10_000 from `from`
5. True ledger debited 100_000 at block N+2; index debited 10_000 → index over-reports `from`'s balance by 90_000 e8s permanently

**Scenario causing index halt:**

1. Block N: `Transfer` at fee = 100_000 e8s → `last_fee = 100_000`
2. Fee drops to 10_000 e8s
3. Block N+1: legacy `Approve` with `fee = None` → index attempts to debit 100_000 from an account whose true balance (as tracked by the index) is only 10_000 → `debit` traps at line 1141–1143 → index canister halts block ingestion

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L134-135)
```rust
    /// This fee is used if no fee nor effetive_fee is found in Approve blocks.
    pub last_fee: Option<Tokens>,
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1037-1037)
```rust
                    mutate_state(|s| s.last_fee = Some(fee));
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1053-1053)
```rust
                    mutate_state(|s| s.last_fee = Some(fee));
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1072-1072)
```rust
                mutate_state(|s| s.last_fee = Some(fee));
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

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1139-1144)
```rust
fn debit(block_index: BlockIndex64, account: Account, amount: Tokens) {
    change_balance(account, |balance| {
        balance.checked_sub(&amount).unwrap_or_else(|| {
            ic_cdk::trap(format!("Block {block_index} caused an underflow for account {account} when calculating balance {balance} - amount {amount}"));
        })
    })
```
