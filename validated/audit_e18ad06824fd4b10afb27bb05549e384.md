Based on my code analysis, here is my assessment:

---

### Title
Allowance Consumed Without Proportional Token Movement via `from == to` in `icrc2_transfer_from` — (`rs/ledger_suite/common/ledger_core/src/approvals.rs`, `rs/ledger_suite/icp/src/lib.rs`, `rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary

An unprivileged spender holding allowance `A` for account `F` can call `icrc2_transfer_from(from=F, to=F, amount=X)`. No guard prevents `from == to`. The allowance is consumed by `X + fee`, but only `fee` tokens are burned from `F`'s balance. The spender can exhaust the entire approved allowance while causing only fee-sized token burns per call.

### Finding Description

**ICP Ledger path** — `icrc1_send_not_async` (`rs/ledger_suite/icp/ledger/src/main.rs:266-370`):

`from` and `to` are both derived from the caller-supplied `from_account` and `to_account` with no equality check. When both resolve to the same `AccountIdentifier`, the code falls into the normal `Transfer` branch. [1](#0-0) 

**`apply_operation` for Transfer** (`rs/ledger_suite/icp/src/lib.rs:203-244`):

The only bypass is for `spender.is_none() || *from == spender.unwrap()` (account owner calling transfer_from on themselves). For a third-party spender (`spender != from`), the code proceeds to check and consume the allowance, then calls `balances.transfer(from, to, amount, fee, None)`. [2](#0-1) 

**`Balances::transfer`** (`rs/ledger_suite/common/ledger_core/src/balances.rs:102-130`):

```
debit(from, amount + fee)   // F loses amount + fee
credit(to, amount)          // F gains amount  (same account!)
token_pool += fee           // fee is burned
```

Net effect on `F`: `-fee`. Net allowance consumed: `X + fee`. There is no check for `from == to`. [3](#0-2) 

**ICRC-1 ledger path** — `execute_transfer_not_async` (`rs/ledger_suite/icrc1/ledger/src/main.rs:570-673`) and `icrc2_transfer_from` (`rs/ledger_suite/icrc1/ledger/src/main.rs:702-725`) have the identical absence of a `from == to` guard. [4](#0-3) 

### Impact Explanation

A spender with allowance `A` can call `transfer_from(from=F, to=F, amount=X)` repeatedly. Each call:
- Burns only `fee` tokens from `F`'s balance
- Consumes `X + fee` from the allowance

By choosing `X` close to `A`, the spender exhausts the entire allowance in a single call while causing only `fee` tokens of actual loss to `F`. The spender receives nothing, making this a pure griefing/denial-of-allowance attack. Any protocol or dApp that relies on the allowance being available (e.g., ckBTC minter flow, SNS swap) can be disrupted.

### Likelihood Explanation

The attack requires only a valid allowance (obtainable by social engineering or as part of a legitimate protocol flow) and a single ingress call. No privileged access, key material, or majority corruption is needed. The path is fully locally testable with a state-machine test.

### Recommendation

Add a guard in both `icrc1_send_not_async` (ICP ledger) and `execute_transfer_not_async` (ICRC-1 ledger) rejecting calls where the resolved `from` account equals the resolved `to` account when a third-party spender is present:

```rust
if spender_account.is_some() && from_account == to_account {
    return Err(CoreTransferError::GenericError { ... });
}
```

Alternatively, add the check at the `apply_operation` level in `rs/ledger_suite/icp/src/lib.rs` before the allowance is consumed.

### Proof of Concept

State-machine test outline:
1. Mint 1_000_000 tokens to account `F`.
2. `F` approves spender `S` for allowance 500_000.
3. `S` calls `icrc2_transfer_from(from=F, to=F, amount=490_000, fee=10_000)`.
4. Assert: `F`'s balance = 990_000 (only fee burned), allowance = 0 (fully consumed by 500_000).
5. `S` has received 0 tokens.

The allowance is fully exhausted while `F` lost only one fee unit instead of 500_000 tokens. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L275-347)
```rust
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
```

**File:** rs/ledger_suite/icp/src/lib.rs (L210-244)
```rust
            if spender.is_none() || *from == spender.unwrap() {
                // It is either a regular transfer or a self-transfer_from.

                // NB. We bypass the allowance check if the account owner calls
                // transfer_from.

                // NB. We cannot reliably detect self-transfer_from at this level.
                // We need help from the transfer_from endpoint to populate
                // [from] and [spender] with equal values if the spender is the
                // account owner.
                context
                    .balances_mut()
                    .transfer(from, to, *amount, *fee, None)?;
                return Ok(());
            }

            let allowance = context.approvals().allowance(from, &spender.unwrap(), now);
            let used_allowance =
                amount
                    .checked_add(fee)
                    .ok_or(TxApplyError::InsufficientAllowance {
                        allowance: allowance.amount,
                    })?;
            if allowance.amount < used_allowance {
                return Err(TxApplyError::InsufficientAllowance {
                    allowance: allowance.amount,
                });
            }
            context
                .balances_mut()
                .transfer(from, to, *amount, *fee, None)?;
            context
                .approvals_mut()
                .use_allowance(from, &spender.unwrap(), used_allowance, now)
                .expect("bug: cannot use allowance");
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L102-130)
```rust
    pub fn transfer(
        &mut self,
        from: &S::AccountId,
        to: &S::AccountId,
        amount: S::Tokens,
        fee: S::Tokens,
        fee_collector: Option<&S::AccountId>,
    ) -> Result<(), BalanceError<S::Tokens>> {
        let debit_amount = amount.checked_add(&fee).ok_or_else(|| {
            // No account can hold more than Tokens::max_value().
            let balance = self.account_balance(from);
            BalanceError::InsufficientFunds { balance }
        })?;
        self.debit(from, debit_amount)?;
        self.credit(to, amount);
        match fee_collector {
            None => {
                // NB. integer overflow is not possible here unless there is a
                // severe bug in the system: total amount of tokens in the
                // circulation cannot exceed Tokens::max_value().
                self.token_pool = self
                    .token_pool
                    .checked_add(&fee)
                    .expect("Overflow while adding the fee to the token pool");
            }
            Some(fee_collector) => self.credit(fee_collector, fee),
        }
        Ok(())
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L702-725)
```rust
async fn icrc2_transfer_from(arg: TransferFromArgs) -> Result<Nat, TransferFromError> {
    let spender_account = Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: arg.spender_subaccount,
    };
    execute_transfer(
        arg.from,
        arg.to,
        Some(spender_account),
        arg.fee,
        arg.amount,
        arg.memo,
        arg.created_at_time,
    )
    .await
    .map_err(convert_transfer_error)
    .map_err(|err| {
        let err: TransferFromError = match err.try_into() {
            Ok(err) => err,
            Err(err) => ic_cdk::trap(&err),
        };
        err
    })
}
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L348-365)
```rust
                        if old_allowance.amount < amount {
                            return Err(InsufficientAllowance(old_allowance.amount));
                        }
                        let mut new_allowance = old_allowance.clone();
                        new_allowance.amount = old_allowance
                            .amount
                            .checked_sub(&amount)
                            .expect("Underflow when using allowance");
                        let rest = new_allowance.amount.clone();
                        if rest.is_zero() {
                            if let Some(expires_at) = old_allowance.expires_at {
                                table.allowances_data.remove_expiry(expires_at, key.clone());
                            }
                            table.allowances_data.remove_allowance(&key);
                        } else {
                            table.allowances_data.set_allowance(key, new_allowance);
                        }
                        Ok(rest)
```
