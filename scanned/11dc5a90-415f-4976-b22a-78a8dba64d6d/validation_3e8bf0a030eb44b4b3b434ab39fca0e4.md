### Title
`TreasuryManagerOperation::new_final` Incorrectly Sets `is_final: false`, Breaking SNS Treasury Audit Trail and Operation Accounting - (File: rs/sns/treasury_manager/src/lib.rs)

### Summary
The `TreasuryManagerOperation::new_final` constructor in the SNS treasury manager library is a copy-paste of `new` and sets `is_final: false` instead of `is_final: true`. Any treasury manager implementation that calls `new_final` to mark the terminal step of a single-step operation will emit incorrect ledger memos and an audit trail that never records any operation as finalized, breaking the accounting invariants the `BalanceBook` is designed to enforce.

### Finding Description
In `rs/sns/treasury_manager/src/lib.rs`, `new_final` is supposed to produce a `TreasuryManagerOperation` whose `Step` carries `is_final: true`. Instead it is byte-for-byte identical to `new`:

```rust
// new — correct
pub fn new(operation: Operation) -> Self {
    Self { operation, step: Step { index: 0, is_final: false } }
}

// new_final — BUG: is_final should be true
pub fn new_final(operation: Operation) -> Self {
    Self { operation, step: Step { index: 0, is_final: false } }  // ← wrong
}
```

The `is_final` flag drives two downstream paths:

1. **Audit-trail display** — `Step::fmt` appends the `-fin` suffix only when `is_final` is `true`. Because it is always `false`, the `Display` impl for `TreasuryManagerOperation` never emits the `-fin` marker.

2. **Ledger memo** — `From<TreasuryManagerOperation> for Vec<u8>` serialises the `Display` output as the on-chain memo. Every ledger transfer that should carry `TreasuryManager.Withdraw-0-fin` instead carries `TreasuryManager.Withdraw-0`, making the final step indistinguishable from an intermediate step.

By contrast, `next_final` (the multi-step variant) correctly sets `is_final: true`, confirming the intent and the copy-paste origin of the defect.

The `BalanceBook` DID contract states that under normal operations `suspense[k] == 0` and `managed_assets[k]` must equal `managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]`. When the final-step memo is wrong, any off-chain reconciliation tool or on-chain logic that gates further state transitions on seeing a `-fin` memo will treat a completed withdrawal as still in-flight, leaving assets in the `suspense` slot and breaking the managed-assets invariant — an exact analog of the vault accounting corruption described in the reference report.

### Impact Explanation
- The SNS treasury audit trail never records any single-step operation (Deposit, Withdraw, IssueReward, Balances) as finalized.
- On-chain or off-chain consumers that key on the `-fin` memo suffix to confirm completion will perpetually see the operation as pending, potentially triggering re-execution of the same withdrawal and double-spending from the external custodian (DEX).
- The `BalanceBook.suspense` field, intended only for transient errors, will accumulate assets that are actually settled, causing `managed_assets` to diverge from reality — the IC analog of the vault share underpricing described in the reference report.
- The corruption persists until manual governance intervention corrects the treasury state, mirroring the reference report's conclusion that "the vault accounting will be invalid until manual interaction of the vault governance."

### Likelihood Explanation
The `TreasuryManager` trait is the mandated interface for all SNS treasury extensions. Any conforming implementation that calls `new_final` for a single-step operation (the common case for atomic Deposit or Withdraw flows) will silently produce wrong memos on every execution. The bug is triggered by normal SNS governance proposals to deposit or withdraw treasury assets — no adversarial input is required; a legitimate SNS token holder submitting a standard `ExtensionOperation` proposal is sufficient.

### Recommendation
Change `new_final` to set `is_final: true`:

```rust
pub fn new_final(operation: Operation) -> Self {
    Self {
        operation,
        step: Step { index: 0, is_final: true },  // was: false
    }
}
```

Add a unit test asserting `TreasuryManagerOperation::new_final(op).step.is_final == true` and that its `Display` output ends with `-fin`.

### Proof of Concept
1. SNS token holders pass a governance proposal executing `ExtensionOperation::TreasuryManagerWithdraw`.
2. SNS Governance calls `treasury_manager.withdraw(request)`.
3. The implementation calls `TreasuryManagerOperation::new_final(Operation::Withdraw)` to stamp the terminal ledger transfer.
4. Because `is_final` is `false`, the ledger memo is `TreasuryManager.Withdraw-0` instead of `TreasuryManager.Withdraw-0-fin`.
5. Any reconciliation logic gating on `-fin` treats the withdrawal as incomplete; assets remain attributed to `suspense` in the `BalanceBook`.
6. `managed_assets` diverges from the true on-chain balance; subsequent `balances` queries return incorrect figures; the SNS treasury accounting is broken until governance manually intervenes. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L317-325)
```rust
impl Display for Step {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.is_final {
            write!(f, "{}-fin", self.index)
        } else {
            write!(f, "{}", self.index)
        }
    }
}
```

**File:** rs/sns/treasury_manager/src/lib.rs (L352-394)
```rust
impl TreasuryManagerOperation {
    pub fn new(operation: Operation) -> Self {
        Self {
            operation,
            step: Step {
                index: 0,
                is_final: false,
            },
        }
    }

    pub fn new_final(operation: Operation) -> Self {
        Self {
            operation,
            step: Step {
                index: 0,
                is_final: false,
            },
        }
    }

    pub fn next(&self) -> Self {
        let index = self.step.index.saturating_add(1);
        Self {
            operation: self.operation,
            step: Step {
                index,
                is_final: false,
            },
        }
    }

    pub fn next_final(&self) -> Self {
        let index = self.step.index.saturating_add(1);
        Self {
            operation: self.operation,
            step: Step {
                index,
                is_final: true,
            },
        }
    }
}
```

**File:** rs/sns/treasury_manager/src/lib.rs (L396-407)
```rust
impl Display for TreasuryManagerOperation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "TreasuryManager.{}-{}", self.operation.name(), self.step)
    }
}

/// To be used for ledger transaction memos.
impl From<TreasuryManagerOperation> for Vec<u8> {
    fn from(operation: TreasuryManagerOperation) -> Self {
        operation.to_string().as_bytes().to_vec()
    }
}
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L156-172)
```text
/// managed_assets[k] == treasury_manager[k] + treasury_owner[k] + external_custodian[k]
///
/// Under "normal operations", the following invariants hold for all k > 0:
/// 1) suspense[k] == 0
/// 2) managed_assets[k] == managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]
type BalanceBook = record {
  treasury_owner : opt Balance;
  treasury_manager : opt Balance;
  external_custodian : opt Balance;
  fee_collector : opt Balance;
  payees : opt Balance;
  payers : opt Balance;

  // An account in which items are entered temporarily before allocation to the correct
  // or final account, e.g., due to transient errors.
  suspense : opt Balance;
};
```

**File:** rs/sns/treasury_manager/mock/src/main.rs (L99-107)
```rust
async fn run_periodic_tasks() {
    log("run_periodic_tasks.");

    let mut state = canister_state();

    state.refresh_balances().await;

    state.issue_rewards().await;
}
```

**File:** rs/sns/governance/src/extensions.rs (L1612-1660)
```rust
/// Execute a treasury manager withdraw operation
async fn execute_treasury_manager_withdraw(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedWithdrawOperationArg,
) -> Result<(), GovernanceError> {
    let arg_blob = construct_treasury_manager_withdraw_payload(arg.original).map_err(|err| {
        GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!("Failed to construct treasury manager withdraw payload: {err}"),
        )
    })?;

    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.withdraw failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!(
                        "Error decoding TreasuryManager.withdraw response: {err:?}"
                    ),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.withdraw failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.withdraw succeeded with response: {:?}",
        balances
    );

    Ok(())
```
