### Title
Unsafe `icrc2_approve` Without `expected_allowance` Enables Allowance Race Condition in SNS Treasury Manager - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The `approve_treasury_manager` function in SNS Governance calls `icrc2_approve` with `expected_allowance: None` for both SNS token and ICP ledgers. An in-code comment incorrectly asserts this is safe. A malicious treasury manager canister can exploit the resulting race condition to drain a leftover allowance from a prior deposit cycle and then also consume the newly approved allowance, spending more treasury funds than governance authorized.

### Finding Description
`approve_treasury_manager` is called in two places: during initial extension registration (`ValidatedRegisterExtension::execute`) and during subsequent deposit operations (`execute_treasury_manager_deposit`). In both cases it calls `icrc2_approve` with `expected_allowance: None`:

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
        None,   // <-- expected_allowance
    )
    .await
``` [1](#0-0) 

The comment's reasoning is that the new allowance Y unconditionally overwrites the old allowance X, so the spender can only ever spend Y. This is only true if the treasury manager cannot spend X *before* the `icrc2_approve` is processed. The ICRC-2 ledger's `approve` implementation confirms: when `expected_allowance` is `None`, the existing allowance is overwritten with no check:

```rust
Some(old_allowance) => {
    if let Some(expected_allowance) = expected_allowance {
        // ... compare-and-swap check ...
    }
    // No check when expected_allowance is None — blindly overwrites
    table.allowances_data.set_allowance(key.clone(), Allowance { amount: amount.clone(), ... });
``` [2](#0-1) 

The `ApproveArgs` struct confirms `expected_allowance` is optional and defaults to `None`: [3](#0-2) 

### Impact Explanation
A malicious treasury manager canister can:

1. Deliberately leave a non-zero residual allowance X after a prior deposit (e.g., by not spending the full approved amount in its `deposit` implementation).
2. Monitor governance proposals via public query calls. When a new deposit proposal for Y tokens passes, submit an `icrc2_transfer_from` to spend X *before* governance executes the proposal.
3. Governance executes the proposal, calling `approve_treasury_manager(Y)` with `expected_allowance=None`, which sets the allowance to Y.
4. The treasury manager's `deposit` function is then called and spends Y.

**Total drained: X + Y. Governance only authorized Y.**

Both SNS tokens and ICP are affected, since both ledger approvals use `expected_allowance: None`: [4](#0-3) 

The deposit flow that triggers this is: [5](#0-4) 

### Likelihood Explanation
- The treasury manager is a governance-approved canister, but once installed it acts autonomously and can be malicious.
- Governance proposals are publicly observable via query calls; the treasury manager can poll proposal state and time its `transfer_from` to land before governance's heartbeat/timer executes the proposal.
- The allowance has a 1-hour expiry window (`ONE_HOUR_SECONDS`), giving ample time for the race.
- The attack requires no privileged access beyond what the treasury manager already legitimately holds.
- The `execute_treasury_manager_deposit` path is the primary attack surface since the treasury manager already has code installed and can call `icrc2_transfer_from` autonomously. [6](#0-5) 

### Recommendation
Replace `expected_allowance: None` with `expected_allowance: Some(0)` (or query the current allowance and pass it as `expected_allowance`). This uses ICRC-2's built-in compare-and-swap protection — the same mechanism the standard provides as the safe alternative to unconditional `approve`. If a non-zero residual allowance exists, the call will return `AllowanceChanged`, alerting governance to the anomaly rather than silently overwriting it.

### Proof of Concept

**Setup:** Treasury manager has been registered and a prior deposit of X SNS tokens was approved. The treasury manager deliberately spent only X/2, leaving X/2 as residual allowance (within the 1-hour expiry).

**Attack steps:**

1. A new `ExecuteExtensionOperation` deposit proposal for Y SNS tokens passes in governance.
2. Treasury manager polls `icrc2_allowance` (query) and confirms residual allowance = X/2.
3. Treasury manager submits `icrc2_transfer_from(from=governance_treasury, to=attacker, amount=X/2)` — this is processed before governance's heartbeat fires.
4. Governance heartbeat fires, calls `execute_treasury_manager_deposit` → `approve_treasury_manager(Y)` → `icrc2_approve(amount=Y, expected_allowance=None)`. Allowance is set to Y.
5. Governance calls `deposit` on the treasury manager. Treasury manager spends Y.

**Result:** Treasury drained of X/2 + Y tokens. Governance only authorized Y.

The root cause is the missing `expected_allowance` guard at: [7](#0-6) 

and: [4](#0-3)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L788-789)
```rust
        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);
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

**File:** rs/sns/governance/src/extensions.rs (L812-820)
```rust
        self.nns_ledger
            .icrc2_approve(
                to,
                icp_amount_e8s,
                Some(expiry_time_nsec),
                icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                self.icp_treasury_subaccount(),
                None,
            )
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1573)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;
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
