### Title
Silent Critical Parameter Changes During ICRC-1 Ledger Upgrade Without Audit Log Entries - (File: `rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The `Ledger::upgrade` function in the ICRC-1 ledger canister silently mutates critical financial parameters — including `transfer_fee`, `fee_collector`, `token_name`, `token_symbol`, `max_memo_length`, `feature_flags`, `archive_options`, and `index_principal` — without emitting any log entries to the canister's log buffer. Off-chain monitors, wallets, and users have no on-canister audit trail to detect or react to these changes.

---

### Finding Description

The `upgrade` function in `rs/ledger_suite/icrc1/ledger/src/lib.rs` is called during `post_upgrade` whenever a controller supplies `UpgradeArgs`. It applies up to eight distinct critical mutations: [1](#0-0) 

None of these branches emit a `log!` call to the `sink` (the canister's `LOG` buffer) to record the old value, the new value, or even the fact that a change occurred. The sole exception is a deprecation warning for the `icrc2` feature flag: [2](#0-1) 

The `post_upgrade` entry point in `rs/ledger_suite/icrc1/ledger/src/main.rs` passes `&LOG` as the sink but does not add any surrounding log statements for the parameter changes either: [3](#0-2) 

The canister does expose a `/logs` HTTP endpoint that serves the `LOG` buffer: [4](#0-3) 

However, because the `upgrade` function writes nothing to `LOG` for these changes, the endpoint provides no evidence that a fee or fee-collector change ever occurred.

---

### Impact Explanation

- **`transfer_fee` change**: Directly alters the cost of every future user transaction. A controller can silently raise fees, extracting more value from users with no on-chain record of when or by how much the fee changed.
- **`change_fee_collector` change**: Redirects where all collected fees are sent. A controller can silently redirect the fee stream to an arbitrary account.
- **`index_principal` change**: Alters which canister the ledger trusts as its index (ICRC-106). A controller can silently point this to a malicious canister.
- **`token_name` / `token_symbol` change**: Allows silent rebranding, which can be used for deception.
- **`change_archive_options` change**: Alters archiving behavior, potentially causing data loss or manipulation of the historical transaction record.

Because the IC's ICRC-1 ledger log buffer is the primary on-canister observability mechanism for off-chain monitors and wallets, the absence of log entries means these changes are invisible until a user or monitor happens to re-query `icrc1_fee()`, `icrc1_metadata()`, etc. and compares against a cached prior value.

---

### Likelihood Explanation

The controller of an ICRC-1 ledger is often an SNS governance canister, which acts on behalf of token holders. A passed governance proposal (which may require only a simple majority) can supply `UpgradeArgs` with any of the above fields set. The upgrade path is a standard, documented, and regularly exercised operation. Any controller — whether an SNS governance canister, a developer-controlled principal, or a multisig — can trigger this silently. The likelihood is **medium**: the operation requires controller access, but controller access is the normal operational path for ledger upgrades, and no additional privilege escalation is needed.

---

### Recommendation

Inside `Ledger::upgrade`, add a `log!(sink, ...)` call for each field that is actually changed, recording both the old and new value. For example:

```rust
if let Some(transfer_fee) = args.transfer_fee {
    let old_fee = self.transfer_fee;
    self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
        ic_cdk::trap(format!("failed to convert transfer fee {transfer_fee} to tokens: {e}"))
    });
    log!(sink, "[upgrade] transfer_fee changed: {:?} -> {:?}", old_fee, self.transfer_fee);
}
```

Apply the same pattern for `fee_collector`, `index_principal`, `token_name`, `token_symbol`, `max_memo_length`, `feature_flags`, and `archive_options`. This ensures the `/logs` endpoint provides a complete, queryable audit trail of all critical parameter changes.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger canister with `transfer_fee = 10_000`.
2. As the controller, call `upgrade_canister` with `UpgradeArgs { transfer_fee: Some(Nat::from(1_000_000_u64)), .. }`.
3. Query `GET /logs` on the canister's HTTP endpoint.
4. Observe: the log contains no entry mentioning the fee change.
5. Query `icrc1_fee()` — it now returns `1_000_000`, but there is no on-canister record of when this changed or what the previous value was.

The relevant code path is: [5](#0-4) 

No `log!` call surrounds this mutation, confirming the silent change.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L913-977)
```rust
    pub fn upgrade(&mut self, sink: impl Sink + Clone, args: UpgradeArgs) {
        if let Some(upgrade_metadata_args) = args.metadata {
            // Only enforce strict validation if existing metadata has no invalid keys.
            // This allows ledgers with legacy invalid keys to still be upgraded.
            let existing_all_valid = self.metadata.iter().all(|(k, _)| k.is_valid());
            self.metadata =
                map_metadata_or_trap(upgrade_metadata_args, existing_all_valid, sink.clone());
        }
        if let Some(token_name) = args.token_name {
            self.token_name = token_name;
        }
        if let Some(token_symbol) = args.token_symbol {
            self.token_symbol = token_symbol;
        }
        if let Some(transfer_fee) = args.transfer_fee {
            self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
                ic_cdk::trap(format!(
                    "failed to convert transfer fee {transfer_fee} to tokens: {e}"
                ))
            });
        }
        if let Some(max_memo_length) = args.max_memo_length {
            if self.max_memo_length > max_memo_length {
                ic_cdk::trap(format!(
                    "The max len of the memo can be changed only to be bigger or equal than the current size. Current size: {}",
                    self.max_memo_length
                ));
            }
            self.max_memo_length = max_memo_length;
        }
        if let Some(change_fee_collector) = args.change_fee_collector {
            self.fee_collector = change_fee_collector.into();
            if self.fee_collector.as_ref().map(|fc| fc.fee_collector) == Some(self.minting_account)
            {
                ic_cdk::trap(
                    "The fee collector account cannot be the same account as the minting account",
                );
            }
        }
        if let Some(feature_flags) = args.feature_flags {
            if !feature_flags.icrc2 {
                log!(
                    sink,
                    "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
                );
            }
            self.feature_flags = feature_flags;
        }
        if let Some(change_archive_options) = args.change_archive_options {
            let mut maybe_archive = self.blockchain.archive.write().expect(
                "BUG: should be unreachable since upgrade has exclusive write access to the ledger",
            );
            if maybe_archive.is_none() {
                ic_cdk::trap(
                    "[ERROR]: Archive options cannot be changed, since there is no archive!",
                );
            }
            if let Some(archive) = maybe_archive.deref_mut() {
                change_archive_options.apply(archive);
            }
        }
        if let Some(index_principal) = args.index_principal {
            self.index_principal = Some(index_principal);
        }
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L244-249)
```rust
            LedgerArgument::Upgrade(upgrade_args) => {
                if let Some(upgrade_args) = upgrade_args {
                    Access::with_ledger_mut(|ledger| ledger.upgrade(&LOG, upgrade_args));
                }
            }
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L483-500)
```rust
    } else if req.path() == "/logs" {
        use std::io::Write;
        let mut buf = vec![];
        for entry in export(&LOG) {
            writeln!(
                &mut buf,
                "{} {}:{} {}",
                entry.timestamp, entry.file, entry.line, entry.message
            )
            .unwrap();
        }
        HttpResponseBuilder::ok()
            .header("Content-Type", "text/plain; charset=utf-8")
            .with_body_and_content_length(buf)
            .build()
    } else {
        HttpResponseBuilder::not_found().build()
    }
```
