### Title
Unsafe Blind Allowance Overwrite in `approve_treasury_manager` Enables Double-Spend of SNS/ICP Treasury Funds - (File: rs/sns/governance/src/extensions.rs)

### Summary
The `approve_treasury_manager` function in SNS Governance calls `icrc2_approve` with `expected_allowance: None`, which causes the ICRC-2 ledger to blindly overwrite any existing non-zero allowance. The code comment explicitly acknowledges this behavior and incorrectly claims it eliminates double-spend risk. In reality, when two `ExecuteExtensionOperation` (deposit) proposals execute concurrently — which is possible because IC canister execution yields at `await` points — the treasury manager canister can spend both the old and the new allowance, draining more treasury funds than any single proposal authorized.

### Finding Description

`approve_treasury_manager` is called in two code paths:

1. `ValidatedRegisterExtension::execute` — during `RegisterExtension` proposal execution
2. `execute_treasury_manager_deposit` — during `ExecuteExtensionOperation` (deposit) proposal execution

In both cases, `icrc2_approve` is called with `expected_allowance: None`:

```rust
// rs/sns/governance/src/extensions.rs:791-802
// If expected_allowance is None, the ledger *blindly* overwrites any existing
// allowance (even if non-zero). Therefore, there is no risk of double spending.

self.ledger
    .icrc2_approve(
        to,
        sns_amount_e8s,
        Some(expiry_time_nsec),
        self.transaction_fee_e8s_or_panic(),
        self.sns_treasury_subaccount(),
        None,  // <-- expected_allowance is None
    )
    .await
```

The comment's reasoning is wrong. The ICRC-2 `approve` with `expected_allowance: None` does atomically overwrite the allowance in a single ledger call. However, the vulnerability arises from the **interleaving of two concurrent proposal executions**:

- SNS Governance spawns proposal execution futures via `start_proposal_execution` using `spawn_in_canister_env`, which runs them concurrently.
- Each `await` point in `execute_treasury_manager_deposit` is a yield point where another proposal's execution can interleave.

**Attack scenario (two concurrent deposit proposals):**

1. Proposal A (deposit 100 SNS) and Proposal B (deposit 200 SNS) are both adopted and begin executing concurrently.
2. Proposal A calls `approve_treasury_manager(treasury_manager, 100_SNS, ...)` → ledger sets allowance to 100.
3. Before Proposal A calls `deposit` on the treasury manager, Proposal B calls `approve_treasury_manager(treasury_manager, 200_SNS, ...)` → ledger **blindly overwrites** allowance to 200.
4. Proposal A now calls `treasury_manager.deposit(100_SNS)` — the treasury manager calls `icrc2_transfer_from` for 100 SNS (succeeds, allowance drops to 100).
5. Proposal B calls `treasury_manager.deposit(200_SNS)` — the treasury manager calls `icrc2_transfer_from` for 200 SNS (succeeds, allowance drops to 0).

**Total drained: 300 SNS, but only 200 SNS was the maximum any single proposal authorized.** The 50%-of-balance validation at proposal submission time does not prevent this because both proposals independently pass the ≤50% check.

The same race applies to ICP allowances via `self.nns_ledger.icrc2_approve(...)`.

### Impact Explanation

An unprivileged SNS token holder who can submit and pass two concurrent `ExecuteExtensionOperation` deposit proposals (or one `RegisterExtension` + one deposit proposal) can cause the SNS treasury to grant the treasury manager canister a combined allowance exceeding what any single governance vote authorized. The treasury manager can then drain both allowances, transferring more SNS tokens and ICP than the DAO intended. This is a **ledger conservation bug** / **governance authorization bypass**: the per-proposal 50%-of-balance cap is circumvented by interleaving two proposals, each of which individually passes validation.

### Likelihood Explanation

The SNS governance system explicitly supports concurrent proposal execution via `spawn_in_canister_env`. Any SNS with an active treasury manager extension and sufficient neuron voting power to pass two deposit proposals in the same voting window is exposed. The treasury manager canister is a new feature (draft API), making this a realistic near-term risk as SNS DAOs begin adopting it. The attacker needs only to be an SNS neuron holder with enough voting power to pass two proposals — no privileged access is required.

### Recommendation

Use `expected_allowance` to implement a compare-and-set pattern. Before calling `icrc2_approve`, read the current allowance and pass it as `expected_allowance`. This ensures the approve fails if another concurrent proposal has already modified the allowance:

```rust
// Read current allowance first
let current = self.ledger.icrc2_allowance(from_account, to).await?;

self.ledger.icrc2_approve(
    to,
    sns_amount_e8s,
    Some(expiry_time_nsec),
    self.transaction_fee_e8s_or_panic(),
    self.sns_treasury_subaccount(),
    Some(current.allowance),  // expected_allowance = current value
).await?;
```

Alternatively, enforce at the governance level that only one treasury manager deposit proposal can execute at a time (a per-extension execution lock), similar to how `UpgradeSnsToNextVersion` uses a lock.

### Proof of Concept

**Root cause — `approve_treasury_manager` with `None` expected_allowance:** [1](#0-0) 

**First call site — `RegisterExtension` proposal execution:** [2](#0-1) 

**Second call site — `ExecuteExtensionOperation` deposit execution:** [3](#0-2) 

**Concurrent proposal execution mechanism (proposals run concurrently via spawn):** [4](#0-3) 

**ICRC-2 ledger `approve` with `None` expected_allowance blindly overwrites existing allowance (confirmed by ledger core logic):** [5](#0-4) 

**The `expected_allowance` field exists precisely to prevent this race — it is `Option<Nat>` and when `None`, no check is performed:** [6](#0-5)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L545-551)
```rust
                    governance
                        .approve_treasury_manager(
                            extension_canister_id,
                            treasury_allocation_sns_e8s,
                            treasury_allocation_icp_e8s,
                        )
                        .await?;
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

**File:** rs/sns/governance/src/governance.rs (L2132-2133)
```rust
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L278-292)
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
```

**File:** packages/icrc-ledger-types/src/icrc2/approve.rs (L17-19)
```rust
    #[serde(default)]
    pub expected_allowance: Option<Nat>,
    #[serde(default)]
```
