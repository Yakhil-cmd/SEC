### Title
ICRC-2 `icrc2_approve` Without `expected_allowance` Enables Allowance Front-Running and Double-Spend — (File: `rs/ledger_suite/common/ledger_core/src/approvals.rs`)

---

### Summary

The IC's ICRC-2 standard (`icrc2_approve` / `icrc2_transfer_from`) replicates the same allowance race condition present in ERC-20. When a token holder calls `icrc2_approve` to reduce an existing spender allowance without supplying the optional `expected_allowance` guard, a malicious spender can race a `icrc2_transfer_from` call against the approval update and drain both the old and the new allowance, transferring more tokens than the holder ever intended.

---

### Finding Description

The IC ledger suite implements ICRC-2 approve/transfer-from semantics. The core allowance logic lives in `rs/ledger_suite/common/ledger_core/src/approvals.rs`. The `approve` function accepts an **optional** `expected_allowance` parameter:

```rust
pub fn approve(
    &mut self,
    account: &AD::AccountId,
    spender: &AD::AccountId,
    amount: AD::Tokens,
    expires_at: Option<TimeStamp>,
    now: TimeStamp,
    expected_allowance: Option<AD::Tokens>,   // ← optional
) -> Result<AD::Tokens, ApproveError<AD::Tokens>>
``` [1](#0-0) 

When `expected_allowance` is `None`, the function unconditionally overwrites the stored allowance with the new value, regardless of what the spender may have already consumed or what the current allowance is:

```rust
Some(old_allowance) => {
    if let Some(expected_allowance) = expected_allowance {
        // compare-and-swap guard — only runs when caller supplies the field
        if expected_allowance != current_allowance {
            return Err(ApproveError::AllowanceChanged { current_allowance });
        }
    }
    // falls through and overwrites unconditionally when expected_allowance == None
    table.allowances_data.set_allowance(key.clone(), Allowance { amount, ... });
``` [2](#0-1) 

Both the ICP ledger endpoint and the ICRC-1 ledger endpoint propagate this optionality directly to callers. The Candid interface declares `expected_allowance : opt Icrc1Tokens`, so any caller that omits the field gets `None`: [3](#0-2) 

The ICRC-1 ledger's `icrc2_approve_not_async` maps `None` straight through:

```rust
let expected_allowance = match arg.expected_allowance {
    Some(n) => Some(n),
    None => None,   // no guard applied
};
``` [4](#0-3) 

The ICP ledger does the same: [5](#0-4) 

---

### Impact Explanation

A malicious spender can transfer **old_allowance + new_allowance** tokens from a victim's account when the victim attempts to reduce the spender's allowance. This is a direct ledger conservation violation: the victim loses more tokens than they authorised. The impact affects every ICRC-2-enabled ledger in the IC ecosystem (ICP ledger, all ckERC-20 ledgers, any third-party ICRC-2 token built on the shared `ledger_core` library). [6](#0-5) 

---

### Likelihood Explanation

The IC does not have a public mempool, so the spender cannot literally observe a pending ingress message the way an Ethereum front-runner can. However, the race window is still reachable:

1. The spender continuously polls `icrc2_allowance` (a cheap query call) to watch for a reduction.
2. The moment the allowance is still at the old value and the holder is known to want to reduce it (e.g., the holder announced intent off-chain, or the spender simply monitors continuously), the spender submits `icrc2_transfer_from` for the full old allowance.
3. Because the IC consensus round (~1–2 s) interleaves ingress messages from different principals in an order the spender cannot control but also cannot be guaranteed to lose, the spender's `icrc2_transfer_from` may be ordered before the holder's `icrc2_approve` within the same round.
4. After the approve lands, the spender submits a second `icrc2_transfer_from` for the new allowance.

This is a realistic attack for any dApp that uses ICRC-2 allowances with a known, semi-trusted counterparty (e.g., a DEX, a lending protocol, or the ckETH minter withdrawal flow where the user approves the minter for a large amount and later tries to reduce it). [7](#0-6) 

---

### Recommendation

1. **Require `expected_allowance` when reducing an existing allowance.** The ledger endpoint should reject an `icrc2_approve` call that would decrease an existing non-zero allowance if `expected_allowance` is absent, mirroring the ERC-20 mitigation of requiring the allowance to be zero before setting a new value.

2. **Document the mandatory use of `expected_allowance` for allowance reductions** in all SDK and dApp developer documentation. The field exists precisely for this purpose but is currently opt-in with no enforcement.

3. **Alternatively**, add an `increaseAllowance` / `decreaseAllowance` pattern (relative adjustments) so that a reduction never races against a concurrent spend.

---

### Proof of Concept

```
// Setup: Alice holds 2000 tokens; Bob has been approved for 1000.
Alice → icrc2_approve(spender=Bob, amount=1000, expected_allowance=None)
// Bob's allowance: 1000

// Alice decides to reduce Bob's allowance to 500.
Alice → icrc2_approve(spender=Bob, amount=500, expected_allowance=None)
//   ↑ this ingress message is in-flight (not yet in a block)

// Bob polls icrc2_allowance and sees allowance=1000 still active.
Bob   → icrc2_transfer_from(from=Alice, to=Bob, amount=1000)
//   ↑ lands in the same consensus round BEFORE Alice's approve

// Result after Alice's approve is processed:
//   Bob has already transferred 1000 tokens.
//   Bob's new allowance = 500.

Bob   → icrc2_transfer_from(from=Alice, to=Bob, amount=500)
// Bob has now transferred 1500 tokens total.
// Alice intended to allow at most 500 after the update.
```

Root cause confirmed at: [8](#0-7)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L232-241)
```rust
    /// Changes the spender's allowance for the account to the specified amount and expiration.
    pub fn approve(
        &mut self,
        account: &AD::AccountId,
        spender: &AD::AccountId,
        amount: AD::Tokens,
        expires_at: Option<TimeStamp>,
        now: TimeStamp,
        expected_allowance: Option<AD::Tokens>,
    ) -> Result<AD::Tokens, ApproveError<AD::Tokens>> {
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L253-307)
```rust
            match table.allowances_data.get_allowance(&key) {
                None => {
                    if let Some(expected_allowance) = expected_allowance
                        && !expected_allowance.is_zero()
                    {
                        return Err(ApproveError::AllowanceChanged {
                            current_allowance: AD::Tokens::zero(),
                        });
                    }
                    if amount == AD::Tokens::zero() {
                        return Ok(amount);
                    }
                    if let Some(expires_at) = expires_at {
                        table.allowances_data.insert_expiry(expires_at, key.clone());
                    }
                    table.allowances_data.set_allowance(
                        key,
                        Allowance {
                            amount: amount.clone(),
                            expires_at,
                            arrived_at: now,
                        },
                    );
                    Ok(amount)
                }
                Some(old_allowance) => {
                    if let Some(expected_allowance) = expected_allowance {
                        let current_allowance = if let Some(expires_at) = old_allowance.expires_at {
                            if expires_at <= now {
                                AD::Tokens::zero()
                            } else {
                                old_allowance.amount.clone()
                            }
                        } else {
                            old_allowance.amount.clone()
                        };
                        if expected_allowance != current_allowance {
                            return Err(ApproveError::AllowanceChanged { current_allowance });
                        }
                    }
                    if amount == AD::Tokens::zero() {
                        if let Some(expires_at) = old_allowance.expires_at {
                            table.allowances_data.remove_expiry(expires_at, key.clone());
                        }
                        table.allowances_data.remove_allowance(&key);
                        return Ok(amount);
                    }
                    table.allowances_data.set_allowance(
                        key.clone(),
                        Allowance {
                            amount: amount.clone(),
                            expires_at,
                            arrived_at: now,
                        },
                    );
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L330-370)
```rust
    /// Consumes amount from the spender's allowance for the account.
    /// Returns an error if the allowance would go negative.
    pub fn use_allowance(
        &mut self,
        account: &AD::AccountId,
        spender: &AD::AccountId,
        amount: AD::Tokens,
        now: TimeStamp,
    ) -> Result<AD::Tokens, InsufficientAllowance<AD::Tokens>> {
        self.with_postconditions_check(|table| {
            let key = (account.clone(), spender.clone());

            match table.allowances_data.get_allowance(&key) {
                None => Err(InsufficientAllowance(AD::Tokens::zero())),
                Some(old_allowance) => {
                    if old_allowance.expires_at.unwrap_or_else(remote_future) <= now {
                        Err(InsufficientAllowance(AD::Tokens::zero()))
                    } else {
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
                    }
                }
            }
        })
    }
```

**File:** rs/ledger_suite/icp/ledger.did (L370-379)
```text
type ApproveArgs = record {
  from_subaccount : opt SubAccount;
  spender : Account;
  amount : Icrc1Tokens;
  expected_allowance : opt Icrc1Tokens;
  expires_at : opt Icrc1Timestamp;
  fee : opt Icrc1Tokens;
  memo : opt blob;
  created_at_time : opt Icrc1Timestamp
};
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L841-855)
```rust
        let expected_allowance = match arg.expected_allowance {
            Some(n) => match Tokens::try_from(n) {
                Ok(n) => Some(n),
                Err(_) => {
                    let current_allowance = ledger
                        .approvals()
                        .allowance(&from_account, &arg.spender, now)
                        .amount;
                    return Err(ApproveError::AllowanceChanged {
                        current_allowance: current_allowance.into(),
                    });
                }
            },
            None => None,
        };
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1351-1367)
```rust
    let expected_allowance = match arg.expected_allowance {
        Some(n) => match n.0.to_u64() {
            Some(n) => Some(Tokens::from_e8s(n)),
            None => {
                let current_allowance = LEDGER
                    .read()
                    .unwrap()
                    .approvals()
                    .allowance(&from, &spender, now)
                    .amount;
                return Err(ApproveError::AllowanceChanged {
                    current_allowance: Nat::from(current_allowance.get_e8s()),
                });
            }
        },
        None => None,
    };
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L221-229)
```text
   │icrc2_approve(minter, tx_fee)│                         │                  │                                          │
   │────────────────────────────>│                         │                  │                                          │
   │               icrc2_approve(minter, amount)           │                  │                                          │
   │──────────────────────────────────────────────────────>│                  │                                          │
   │                             │                         │                  │                                          │
   │                             │                         │                  │                                          │
   │                             │                         │                  │                                          │
   │    withdraw_erc20(ckerc20_ledger_id, amount, destination_eth_address)    │                                          │
   │─────────────────────────────────────────────────────────────────────────>│                                          │
```
