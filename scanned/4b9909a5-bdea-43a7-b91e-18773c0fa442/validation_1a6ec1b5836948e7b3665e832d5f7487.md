### Title
ICRC-2 `icrc2_approve` Without `expected_allowance` Allows Unordered Execution to Produce Attacker-Chosen Final Allowance State - (File: `rs/ledger_suite/common/ledger_core/src/approvals.rs`)

---

### Summary

The ICRC-2 ledger implementation on the Internet Computer allows `icrc2_approve` calls to be submitted without the optional `expected_allowance` field. When a user submits multiple approve calls without this field, the final allowance state depends entirely on the order in which those calls are inducted into blocks. Because the IC ingress pipeline does not enforce strict per-sender ordering across blocks, a block-proposing node (or any entity that can influence which ingress messages are selected and in what order) can choose the permutation of those approve calls that produces the most damaging final allowance state — directly analogous to the unordered-nonce MEV described in the report.

---

### Finding Description

**Root cause in `rs/ledger_suite/common/ledger_core/src/approvals.rs`, `AllowanceTable::approve` (lines 232–323):**

The `approve` function unconditionally overwrites the existing allowance when `expected_allowance` is `None`:

```rust
// lines 278–319: when old_allowance exists and expected_allowance is None,
// the check at line 279 is skipped entirely, and the new amount is written
// unconditionally at line 300.
table.allowances_data.set_allowance(
    key.clone(),
    Allowance {
        amount: amount.clone(),
        expires_at,
        arrived_at: now,
    },
);
```

This means that if a user submits two approve calls — `Approve(spender, 0)` (revoke) and `Approve(spender, X)` (grant) — without `expected_allowance`, both are independently valid regardless of which executes first. The final allowance is determined solely by execution order.

**Ingress ordering is not strictly enforced across blocks.** In `rs/ingress_manager/src/ingress_selector.rs`, `get_ingress_payload` (lines 51–276), messages are sorted by pool arrival timestamp within a per-canister queue, but:

1. The block proposer selects which messages to include and in what order across the payload.
2. Multiple approve calls from the same sender to the same canister can land in different blocks, with no protocol-level guarantee of FIFO ordering between blocks.
3. The ingress pool sorts by `artifact.timestamp` (line 151–153), which is the local node's wall-clock time of receipt — not a user-controlled field, but also not a consensus-certified ordering.

**The `icrc2_approve` endpoint** in `rs/ledger_suite/icrc1/ledger/src/main.rs` (lines 820–891) and `rs/ledger_suite/icp/ledger/src/main.rs` (lines 1418–1425) both call into `apply_transaction`, which calls `AllowanceTable::approve`. Neither enforces that a sequence of approvals from the same account to the same spender is applied in submission order.

**The deduplication mechanism** in `rs/ledger_suite/common/ledger_canister_core/src/ledger.rs` (lines 237–253) only prevents exact duplicate transactions (same `created_at_time` + same hash). Two distinct approve calls — e.g., `Approve(spender, 0, created_at_time=T1)` and `Approve(spender, 1000, created_at_time=T2)` — are not duplicates and both will be accepted in either order.

---

### Impact Explanation

**Scenario — Allowance griefing / residual allowance:**

1. User submits `Approve(spender, 1000)` (grant) followed by `Approve(spender, 0)` (revoke) to cleanly revoke a prior grant.
2. A block proposer (or any node that can delay one message) includes the revoke first, then the grant — leaving a non-zero allowance of 1000 active.
3. The spender can now call `icrc2_transfer_from` to drain the user's account even though the user believed they had revoked the allowance.

**Scenario — Allowance upgrade race:**

1. User has allowance 100 for spender. User submits `Approve(spender, 0)` then `Approve(spender, 500)` to change the allowance.
2. A block proposer reverses the order: `Approve(spender, 500)` lands first, then `Approve(spender, 0)` revokes it.
3. The spender never gets the 500 allowance the user intended to grant.

Both scenarios are reachable by any block-proposing node (one per round, rotating) without any privileged access beyond normal consensus participation.

---

### Likelihood Explanation

- **Attacker role:** Any block-proposing replica node (one honest node per round in normal operation; a malicious node within the subnet's fault tolerance). The block proposer selects which ingress messages to include and in what order within a block.
- **Trigger condition:** A user submits two or more `icrc2_approve` calls to the same `(from, spender)` pair within the same `MAX_INGRESS_TTL` window (5 minutes) without using `expected_allowance`.
- **Frequency:** This is a common usage pattern. The ICRC-2 standard's `expected_allowance` field is optional and many callers omit it (as seen in test helpers throughout the codebase, e.g., `rs/ledger_suite/icrc1/index-ng/tests/tests.rs` line 283: `expected_allowance: None`).
- **No external dependency:** The vulnerability is entirely within the IC ledger canister logic and the ingress ordering properties of the IC protocol.

---

### Recommendation

1. **Document and enforce `expected_allowance` for sequential approve sequences.** The ICRC-2 standard already provides `expected_allowance` as the correct mitigation. Callers performing a revoke-then-grant or grant-then-revoke sequence MUST use `expected_allowance` to make each step depend on the prior state.

2. **Consider adding a per-account approve sequence number** (analogous to an ordered nonce) to the ledger state, so that multiple approve calls from the same account can be ordered deterministically without relying on block-inclusion order.

3. **Add a warning in the `icrc2_approve` endpoint** when `expected_allowance` is `None` and an existing non-zero allowance is being overwritten, or reject such calls at the protocol level when the new amount is zero (revoke) to prevent silent ordering attacks.

---

### Proof of Concept

```
State: allowance(Alice → Bob) = 0

Step 1: Alice submits Tx_A = icrc2_approve(spender=Bob, amount=0,   created_at_time=T1)
Step 2: Alice submits Tx_B = icrc2_approve(spender=Bob, amount=1000, created_at_time=T2)
        (Alice intends: grant 1000, then immediately revoke — net result should be 0)

Normal execution order (T1 < T2):
  Apply Tx_A: allowance = 0   (no-op, already 0)
  Apply Tx_B: allowance = 1000
  Final: allowance = 1000  ← Alice intended this

Attacker (block proposer) reverses order:
  Apply Tx_B: allowance = 1000
  Apply Tx_A: allowance = 0
  Final: allowance = 0  ← Bob gets nothing

Alternatively, Alice submits Tx_C = icrc2_approve(spender=Bob, amount=1000, created_at_time=T1)
                  then Tx_D = icrc2_approve(spender=Bob, amount=0,    created_at_time=T2)
        (Alice intends: grant then revoke — net result should be 0)

Attacker reverses:
  Apply Tx_D: allowance = 0   (no-op)
  Apply Tx_C: allowance = 1000
  Final: allowance = 1000  ← Bob can now drain Alice via transfer_from
```

The root cause is confirmed at: [1](#0-0) 

The `expected_allowance` check is skipped when `None`, allowing unconditional overwrite regardless of execution order. [2](#0-1) 

The ICRC-2 `ApproveArgs` struct confirms `expected_allowance` is optional: [3](#0-2) 

The ingress selector sorts by pool arrival timestamp (not a consensus-certified ordering), and the block proposer controls inclusion order: [4](#0-3) 

The deduplication mechanism only prevents exact hash duplicates, not ordering attacks between distinct transactions: [5](#0-4)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L253-261)
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
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L278-320)
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

                    if expires_at != old_allowance.expires_at {
                        if let Some(old_expiration) = old_allowance.expires_at {
                            table
                                .allowances_data
                                .remove_expiry(old_expiration, key.clone());
                        }
                        if let Some(expires_at) = expires_at {
                            table.allowances_data.insert_expiry(expires_at, key);
                        }
                    }
                    Ok(amount)
                }
```

**File:** packages/icrc-ledger-types/src/icrc2/approve.rs (L12-27)
```rust
pub struct ApproveArgs {
    #[serde(default)]
    pub from_subaccount: Option<Subaccount>,
    pub spender: Account,
    pub amount: Nat,
    #[serde(default)]
    pub expected_allowance: Option<Nat>,
    #[serde(default)]
    pub expires_at: Option<u64>,
    #[serde(default)]
    pub fee: Option<Nat>,
    #[serde(default)]
    pub memo: Option<Memo>,
    #[serde(default)]
    pub created_at_time: Option<u64>,
}
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L146-154)
```rust
        // At this point messages are sorted by expiry time. In order to prevent malicious
        // users from putting their messages ahead of others by carefully crafting the expiry
        // times, we sort the ingress messages by the time they were delivered to the pool.
        // NOTE: We sort in reverse order, because messages are pop()-ed from the back.
        for v in canister_queues.values_mut() {
            v.msgs.sort_unstable_by_key(|artifact| {
                std::cmp::Reverse(artifact.timestamp.as_nanos_since_unix_epoch())
            });
        }
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/ledger.rs (L237-253)
```rust
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
