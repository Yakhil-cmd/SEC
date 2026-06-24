### Title
`send_whitelist` Restriction Completely Ineffective — Non-Whitelisted Principals Can Always Send ICP Tokens - (File: `rs/ledger_suite/icp/ledger/src/lib.rs`)

---

### Summary

The ICP ledger stores a `send_whitelist` field explicitly documented as "Used to prevent non-whitelisted canisters from sending tokens," but the enforcement function `can_send()` unconditionally returns `true`, making the whitelist a dead letter. Additionally, the ICRC-1/ICRC-2 transfer path never calls `can_send()` at all. Any principal — whitelisted or not — can freely send ICP tokens through either the legacy `send` endpoint or the ICRC-1 `icrc1_transfer` endpoint.

---

### Finding Description

The `Ledger` struct in `rs/ledger_suite/icp/ledger/src/lib.rs` carries:

```rust
/// Used to prevent non-whitelisted canisters from sending tokens.
send_whitelist: HashSet<CanisterId>,
``` [1](#0-0) 

The enforcement point is `can_send()`:

```rust
pub fn can_send(&self, _principal_id: &PrincipalId) -> bool {
    true
}
``` [2](#0-1) 

The parameter is named `_principal_id` (prefixed with `_` to suppress the unused-variable warning), the `send_whitelist` field is never consulted, and the function always returns `true`. The legacy `send` endpoint calls this function but the check is a no-op:

```rust
if !LEDGER.read().unwrap().can_send(&caller_principal_id) {
    panic!("Sending from {caller_principal_id} is not allowed");
}
``` [3](#0-2) 

The ICRC-1 path (`icrc1_send_not_async`) does not call `can_send()` at all — it proceeds directly to `apply_transaction` without any whitelist check: [4](#0-3) 

The `send_whitelist` is only actually consulted in `can_be_notified()`, which gates the now-deprecated `notify` flow — not token sending:

```rust
pub fn can_be_notified(&self, canister_id: &CanisterId) -> bool {
    LEDGER.read().unwrap().send_whitelist.contains(canister_id)
}
``` [5](#0-4) 

The `send_whitelist` is accepted as an init argument and stored: [6](#0-5) 

But it is never read back for the purpose of restricting sends.

---

### Impact Explanation

Any canister or user principal — regardless of whether it appears in the `send_whitelist` — can call either `send`/`send_pb`/`transfer` (legacy) or `icrc1_transfer`/`icrc2_transfer_from` (ICRC-1/2) and move ICP tokens freely. An operator or governance proposal that configures a non-empty `send_whitelist` expecting it to restrict token movement receives no enforcement whatsoever. The restriction is silently bypassed on every transfer path.

**Impact: Low** (the ICP ledger is a public token ledger and the whitelist was likely repurposed; however, the stated contract of the field is violated and any deployment relying on it is silently unprotected)

---

### Likelihood Explanation

**Likelihood: High** — every ICP transfer call exercises this code path. No special conditions are required; any unprivileged ingress sender or canister caller can invoke `icrc1_transfer` or the legacy `send` endpoint and bypass the whitelist unconditionally.

---

### Recommendation

Either:
1. Remove the `send_whitelist` field and its init argument entirely if it is no longer intended to gate sends (it is already unused for that purpose), and update the comment/documentation accordingly; or
2. Implement the whitelist check inside `can_send()` by consulting `self.send_whitelist`, and extend the check to the ICRC-1/ICRC-2 transfer path (`icrc1_send_not_async`) which currently bypasses it entirely.

The `can_be_notified()` function should be renamed or separated from `send_whitelist` to avoid further confusion between the two semantics.

---

### Proof of Concept

1. Deploy the ICP ledger with a non-empty `send_whitelist` containing only canister `A`.
2. From canister `B` (not in the whitelist), call `icrc1_transfer` to transfer ICP to any account.
3. The transfer succeeds — `can_send()` returns `true` unconditionally and `icrc1_send_not_async` never checks the whitelist.
4. Alternatively, call the legacy `send_pb` endpoint from any user principal not in the whitelist — `can_send()` is called but returns `true`, so the panic branch is never reached. [7](#0-6) [4](#0-3)

### Citations

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L213-214)
```rust
    /// Used to prevent non-whitelisted canisters from sending tokens.
    send_whitelist: HashSet<CanisterId>,
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L468-493)
```rust
        send_whitelist: HashSet<CanisterId>,
        transfer_fee: Option<Tokens>,
        token_symbol: Option<String>,
        token_name: Option<String>,
        feature_flags: Option<FeatureFlags>,
    ) {
        self.token_symbol = token_symbol.unwrap_or_else(|| "ICP".to_string());
        self.token_name = token_name.unwrap_or_else(|| "Internet Computer".to_string());
        self.balances.token_pool = Tokens::MAX;
        self.minting_account_id = Some(minting_account);
        self.icrc1_minting_account = icrc1_minting_account;
        if let Some(t) = transaction_window {
            self.transaction_window = t;
        }

        for (to, amount) in initial_values.into_iter() {
            self.add_payment_with_timestamp(
                Memo::default(),
                Operation::Mint { to, amount },
                None,
                timestamp,
            )
            .unwrap_or_else(|_| panic!("Creating account {to:?} failed"));
        }

        self.send_whitelist = send_whitelist;
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L546-548)
```rust
    pub fn can_send(&self, _principal_id: &PrincipalId) -> bool {
        true
    }
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L550-554)
```rust
    /// Check if it's allowed to notify this canister.
    /// Currently we reuse whitelist for that.
    pub fn can_be_notified(&self, canister_id: &CanisterId) -> bool {
        LEDGER.read().unwrap().send_whitelist.contains(canister_id)
    }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L192-264)
```rust
async fn send(
    memo: Memo,
    amount: Tokens,
    fee: Tokens,
    from_subaccount: Option<Subaccount>,
    to: AccountIdentifier,
    created_at_time: Option<TimeStamp>,
) -> Result<BlockIndex, TransferError> {
    let caller_principal_id = PrincipalId::from(caller());

    if !LEDGER.read().unwrap().can_send(&caller_principal_id) {
        panic!("Sending from {caller_principal_id} is not allowed");
    }

    let from = AccountIdentifier::new(caller_principal_id, from_subaccount);
    let minting_acc = LEDGER
        .read()
        .unwrap()
        .minting_account_id
        .expect("Minting canister id not initialized");

    let transfer = if from == minting_acc {
        assert_eq!(fee, Tokens::ZERO, "Fee for minting should be zero");
        assert_ne!(
            to, minting_acc,
            "It is illegal to mint to a minting_account"
        );
        Operation::Mint { to, amount }
    } else if to == minting_acc {
        assert_eq!(fee, Tokens::ZERO, "Fee for burning should be zero");
        let balance = LEDGER.read().unwrap().balances().account_balance(&from);
        let min_burn_amount = LEDGER.read().unwrap().transfer_fee.min(balance);
        if amount < min_burn_amount {
            panic!("Burns lower than {min_burn_amount} are not allowed");
        }
        Operation::Burn {
            from,
            amount,
            spender: None,
        }
    } else {
        let transfer_fee = LEDGER.read().unwrap().transfer_fee;
        if fee != transfer_fee {
            return Err(TransferError::BadFee {
                expected_fee: transfer_fee,
            });
        }
        Operation::Transfer {
            from,
            to,
            spender: None,
            amount,
            fee,
        }
    };
    let (height, hash) = match LEDGER
        .write()
        .unwrap()
        .add_payment(memo, transfer, created_at_time)
    {
        Ok((height, hash)) => (height, hash),
        Err(PaymentError::TransferError(transfer_error)) => return Err(transfer_error),
        Err(PaymentError::Reject(msg)) => panic!("{}", msg),
    };
    certified_data_set(hash.into_bytes());

    // Don't put anything that could ever trap after this call or people using this
    // endpoint. If something did panic the payment would appear to fail, but would
    // actually succeed on chain.
    let max_msg_size = *MAX_MESSAGE_SIZE_BYTES.read().unwrap();
    archive_blocks::<Access>(DebugOutSink, max_msg_size as u64).await;
    Ok(height)
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L266-370)
```rust
fn icrc1_send_not_async(
    memo: Option<icrc_ledger_types::icrc1::transfer::Memo>,
    amount: Nat,
    fee: Option<Nat>,
    from_account: Account,
    to_account: Account,
    spender_account: Option<Account>,
    created_at_time: Option<u64>,
) -> Result<BlockIndex, CoreTransferError<Tokens>> {
    let from = AccountIdentifier::from(from_account);
    let to = AccountIdentifier::from(to_account);
    match memo.as_ref() {
        Some(memo) if memo.0.len() > MEMO_SIZE_BYTES => trap("the memo field is too large"),
        _ => {}
    };
    let amount = match amount.0.to_u64() {
        Some(n) => Tokens::from_e8s(n),
        None => {
            // No one can have so many tokens
            let balance = account_balance(from);
            assert!(balance.get_e8s() < amount);
            return Err(CoreTransferError::InsufficientFunds { balance });
        }
    };
    let created_at_time = created_at_time.map(TimeStamp::from_nanos_since_unix_epoch);
    let minting_acc = LEDGER
        .read()
        .unwrap()
        .minting_account_id
        .expect("Minting canister id not initialized");
    let now = TimeStamp::from_nanos_since_unix_epoch(time());
    let (operation, effective_fee) = if to == minting_acc {
        if fee.is_some() && fee.as_ref() != Some(&Nat::from(0_u64)) {
            return Err(CoreTransferError::BadFee {
                expected_fee: Tokens::ZERO,
            });
        }
        let ledger = LEDGER.read().unwrap();
        let balance = ledger.balances().account_balance(&from);
        let min_burn_amount = ledger.transfer_fee.min(balance);
        if amount < min_burn_amount {
            return Err(CoreTransferError::BadBurn { min_burn_amount });
        }
        if amount == Tokens::ZERO {
            return Err(CoreTransferError::BadBurn {
                min_burn_amount: ledger.transfer_fee,
            });
        }
        (
            Operation::Burn {
                from,
                amount,
                spender: spender_account.map(AccountIdentifier::from),
            },
            Tokens::ZERO,
        )
    } else if from == minting_acc {
        if spender_account.is_some() {
            trap("the minter account cannot delegate mints");
        }
        if fee.is_some() && fee.as_ref() != Some(&Nat::from(0_u64)) {
            return Err(CoreTransferError::BadFee {
                expected_fee: Tokens::ZERO,
            });
        }
        (Operation::Mint { to, amount }, Tokens::ZERO)
    } else {
        let expected_fee = LEDGER.read().unwrap().transfer_fee;
        if fee.is_some() && fee.as_ref() != Some(&Nat::from(expected_fee.get_e8s())) {
            return Err(CoreTransferError::BadFee { expected_fee });
        }
        (
            Operation::Transfer {
                from,
                to,
                spender: spender_account.map(AccountIdentifier::from),
                amount,
                fee: expected_fee,
            },
            expected_fee,
        )
    };

    let block_index = {
        let mut ledger = LEDGER.write().unwrap();
        let tx = Transaction {
            operation,
            memo: Memo(0),
            icrc1_memo: memo.map(|x| x.0),
            created_at_time,
        };

        #[cfg(not(feature = "canbench-rs"))]
        let (block_index, hash) = apply_transaction(&mut *ledger, tx, now, effective_fee)?;

        #[cfg(feature = "canbench-rs")]
        let (block_index, _hash) = apply_transaction(&mut *ledger, tx, now, effective_fee)?;

        #[cfg(not(feature = "canbench-rs"))]
        certified_data_set(hash.into_bytes());

        block_index
    };
    Ok(block_index)
}
```
