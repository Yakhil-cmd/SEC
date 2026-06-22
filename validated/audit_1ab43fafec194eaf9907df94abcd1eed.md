### Title
ICRC-2 `icrc2_approve` Without `expected_allowance` Enables Allowance Double-Spend Race Condition — (`rs/ledger_suite/common/ledger_core/src/approvals.rs`, `rs/ledger_suite/icp/ledger/src/main.rs`)

---

### Summary

The IC's ICRC-2 ledger implementation exposes the same allowance-change race condition as the PSP22 `approve` vulnerability. The `expected_allowance` field in `icrc2_approve` is optional; when omitted, the ledger unconditionally overwrites the existing allowance. A spender who learns of a pending allowance reduction can submit `icrc2_transfer_from` for the full old allowance before the owner's `icrc2_approve` is processed, then spend the newly set allowance as well — consuming old + new instead of only new. The ICP ledger's dedicated `remove_approval` endpoint hardcodes `expected_allowance: None`, making revocation attempts silently raceable. Additionally, a comment in SNS governance's `approve_treasury_manager` incorrectly asserts that omitting `expected_allowance` eliminates double-spend risk, which is the opposite of the truth.

---

### Finding Description

**Root cause — `AllowanceTable::approve` with `expected_allowance = None`**

In `rs/ledger_suite/common/ledger_core/src/approvals.rs`, the shared `approve` method accepts an optional `expected_allowance`: [1](#0-0) 

When `expected_allowance` is `None`, the branch at line 278 skips the current-value check entirely and unconditionally overwrites the stored allowance: [2](#0-1) 

The guard only fires when the caller explicitly supplies `Some(expected)`: [3](#0-2) 

**ICP ledger `icrc2_approve` endpoint passes `None` through unchanged**

`icrc2_approve_not_async` in `rs/ledger_suite/icp/ledger/src/main.rs` maps a missing caller-supplied `expected_allowance` directly to `None`, so the guard is never triggered for ordinary callers who omit the field: [4](#0-3) 

The ICRC-1 ledger (`rs/ledger_suite/icrc1/ledger/src/main.rs`) has identical logic: [5](#0-4) 

**`remove_approval` hardcodes `expected_allowance: None`**

The ICP ledger's dedicated revocation endpoint constructs its internal `ApproveArgs` with `expected_allowance: None` and `amount: 0`: [6](#0-5) 

Because no current-value check is performed, a spender can race `icrc2_transfer_from` against the owner's `remove_approval` call and drain the full outstanding allowance before the revocation lands.

**SNS governance `approve_treasury_manager` contains an inverted security comment**

`rs/sns/governance/src/extensions.rs` passes `None` for `expected_allowance` and justifies it with a comment that reverses the actual security property: [7](#0-6) 

The comment claims "there is no risk of double spending" precisely because `expected_allowance` is `None`. This is incorrect: omitting `expected_allowance` is what *enables* the race, not what prevents it. The blind overwrite means the approve will not fail if the allowance was already partially or fully consumed, but it does not prevent the spender from consuming both the old and new allowance.

---

### Impact Explanation

A malicious spender can spend `old_allowance + new_allowance` tokens instead of only `new_allowance` tokens whenever an account owner changes a non-zero allowance to another non-zero value without supplying `expected_allowance`. For `remove_approval`, the spender can spend the entire outstanding allowance that the owner intended to revoke. This is a direct, unrecoverable financial loss to the account owner. The ICP and ICRC-1 ledgers are production system canisters holding real value (ICP, ckBTC, ckETH, SNS tokens), so the impact is concrete ledger conservation breakage.

---

### Likelihood Explanation

The attack does not require mempool visibility. The IC's ingress pool is not strictly ordered; when two messages targeting the same canister are submitted close together, the block maker determines their relative order. A spender who learns of an impending allowance reduction through any off-chain channel (DeFi protocol UI, governance proposal, direct communication) can immediately submit `icrc2_transfer_from` for the old amount. If the spender's message is included in the same block before the owner's `icrc2_approve`, the race succeeds. The `expected_allowance` field is optional and widely omitted — the IC's own test helpers, index-ng tests, and the `remove_approval` production endpoint all omit it — making the unprotected code path the common case rather than the exception. [8](#0-7) 

---

### Recommendation

1. **`remove_approval`**: Supply `expected_allowance` equal to the current allowance read immediately before constructing the `ApproveArgs`, or document that callers must accept the race and handle the case where the spender has already consumed the allowance.
2. **`approve_treasury_manager`**: Correct the comment. If the double-spend risk is accepted because the treasury manager is a trusted canister, state that explicitly. If it is not accepted, pass the current allowance as `expected_allowance`.
3. **Protocol-level guidance**: The ICRC-2 standard should be updated to strongly recommend (SHOULD → MUST for non-zero-to-non-zero transitions) that callers supply `expected_allowance` when changing an existing non-zero allowance, mirroring the ERC-20 community's established guidance.

---

### Proof of Concept

**Scenario (ICP ledger, `remove_approval` race):**

1. Alice holds 2 000 ICP. She calls `icrc2_approve(Bob, 1000)` — no `expected_allowance` needed for a fresh approval.
2. Alice later calls `remove_approval(Bob)`. Internally this constructs `ApproveArgs { amount: 0, expected_allowance: None, … }`.
3. Bob observes Alice's intent (off-chain) and immediately submits `icrc2_transfer_from(Alice → Bob, 1000)`.
4. The IC block maker includes Bob's `icrc2_transfer_from` before Alice's `remove_approval` in the same block (ordering is not guaranteed to be submission-time FIFO).
5. Bob's transfer executes: allowance drops from 1 000 to 0, Bob receives 1 000 ICP.
6. Alice's `remove_approval` executes: it calls `approve(amount=0, expected_allowance=None)`. The allowance is already 0; the call succeeds silently as a no-op.
7. Net result: Bob spent 1 000 ICP that Alice intended to revoke. Alice has no recourse.

**Scenario (ICRC-1 ledger, allowance-change race):**

1. Alice approves Bob for 1 000 tokens: `icrc2_approve(Bob, 1000, expected_allowance=None)`.
2. Alice submits `icrc2_approve(Bob, 500, expected_allowance=None)` to reduce the allowance.
3. Bob front-runs with `icrc2_transfer_from(Alice → Bob, 1000)` — succeeds, allowance → 0.
4. Alice's approve lands: allowance set to 500 unconditionally (no check, `expected_allowance` is `None`).
5. Bob submits `icrc2_transfer_from(Alice → Bob, 500)` — succeeds.
6. Total drained: 1 500 tokens. Alice intended to allow only 500. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L233-241)
```rust
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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L278-307)
```rust
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

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1438-1460)
```rust
async fn remove_approval(args: RemoveApprovalArgs) -> Result<Nat, ApproveError> {
    let approve_arg = ApproveArgs {
        from_subaccount: args.from_subaccount,
        spender: Account {
            owner: Principal::anonymous(),
            subaccount: None,
        },
        amount: Nat::from(0_u64),
        expected_allowance: None,
        expires_at: None,
        fee: args.fee,
        memo: None,
        created_at_time: None,
    };
    let spender = AccountIdentifier::from_address(args.spender).unwrap_or_else(|e| {
        trap(format!("Invalid account identifier: {e}"));
    });
    let block_index = icrc2_approve_not_async(caller(), approve_arg, Some(spender))?;

    let max_msg_size = *MAX_MESSAGE_SIZE_BYTES.read().unwrap();
    archive_blocks::<Access>(DebugOutSink, max_msg_size as u64).await;
    Ok(block_index)
}
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

**File:** rs/sns/governance/src/extensions.rs (L791-802)
```rust
        // If expected_allowance is None, the ledger *blindly* overwrites any existing
        // allowance (even if non-zero). Therefore, there is no risk of double spending.

        self.ledger
            .icrc2_approve(
                to,
                sns_amount_e8s,
                Some(expiry_time_nsec),
                self.transaction_fee_e8s_or_panic(),
                self.sns_treasury_subaccount(),
                None,
            )
```

**File:** rs/ledger_suite/icrc1/index-ng/tests/tests.rs (L279-289)
```rust
    let req = ApproveArgs {
        from_subaccount: from.subaccount,
        spender,
        amount: Nat::from(amount),
        expected_allowance: None,
        expires_at: None,
        fee: None,
        memo: None,
        created_at_time: None,
    };
    icrc2_approve(env, ledger, PrincipalId(from.owner), req)
```
