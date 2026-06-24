### Title
`TreasuryManagerOperation::new_final` Hardcodes Wrong `is_final` Flag, Corrupting Audit Trail and Ledger Memos - (File: `rs/sns/treasury_manager/src/lib.rs`)

---

### Summary

`TreasuryManagerOperation::new_final` is supposed to construct a "final-step" operation (index 0, `is_final = true`), but it hardcodes `is_final: false`, making it byte-for-byte identical to `new()`. Any Treasury Manager implementation that calls `new_final` will emit incorrect ledger transaction memos and corrupt the on-chain audit trail, because the `-fin` suffix is never appended. This is the direct IC analog of the external report's "wrong modality" class: a constructor that silently selects the wrong flag for a multi-step operation.

---

### Finding Description

In `rs/sns/treasury_manager/src/lib.rs`, the `TreasuryManagerOperation` type models multi-step treasury operations (Deposit, Withdraw, IssueReward, Balances). Each step carries an `is_final: bool` that distinguishes intermediate steps from the concluding step of an operation. [1](#0-0) 

The `Display` implementation branches on `is_final` to produce either `"{index}-fin"` (final) or `"{index}"` (intermediate): [2](#0-1) 

This formatted string is then used directly as the **ledger transaction memo** for every transfer the Treasury Manager makes: [3](#0-2) 

The bug is in `new_final`: it sets `is_final: false` instead of `is_final: true`, making it functionally identical to `new()`: [4](#0-3) 

Compare with `next_final`, which correctly sets `is_final: true`: [5](#0-4) 

The DID specification and inline documentation explicitly show that the final step of a Deposit operation must have `is_final = true` at `index = 1`: [6](#0-5) 

---

### Impact Explanation

Any Treasury Manager implementation that calls `new_final` to mark the concluding step of an operation will:

1. **Corrupt ledger memos**: The memo written to the ICP or SNS ledger will read `TreasuryManager.Deposit-0` instead of `TreasuryManager.Deposit-0-fin`, making it impossible to distinguish final steps from intermediate steps in the immutable ledger history.
2. **Corrupt the on-chain audit trail**: The `audit_trail` query endpoint returns `Transaction` records whose `treasury_manager_operation` field will have `is_final = false` for what should be final steps. Financial auditors and governance participants relying on this trail to verify operation completion will receive incorrect data.
3. **Potential logic errors**: Any Treasury Manager implementation that gates post-operation cleanup or state transitions on `is_final` (e.g., releasing a lock, marking an operation complete, or preventing re-entry) will malfunction silently, since `new_final` never sets the flag.

The SNS Treasury Manager manages real SNS and ICP treasury assets on behalf of DAOs. Corrupted audit trails and memos directly undermine the financial accountability guarantees the system is designed to provide. [7](#0-6) 

---

### Likelihood Explanation

`new_final` is a public API of the `sns_treasury_manager` library crate, exported for use by any Treasury Manager implementer. The function name strongly implies correct behavior, so implementers will call it without inspecting its body. The bug is latent today (no call sites exist yet in this repo), but will be triggered by the first production Treasury Manager implementation that uses `new_final` to mark a single-step or first-step-as-final operation. The SNS extension framework is actively being built out, making this a near-term risk. [8](#0-7) 

---

### Recommendation

Change `new_final` to set `is_final: true`:

```rust
pub fn new_final(operation: Operation) -> Self {
    Self {
        operation,
        step: Step {
            index: 0,
            is_final: true,  // was: false
        },
    }
}
```

Add a unit test asserting `TreasuryManagerOperation::new_final(op).step.is_final == true` and that its `Display` output contains `-fin`.

---

### Proof of Concept

```rust
use sns_treasury_manager::{TreasuryManagerOperation, Operation};

fn main() {
    let op_new       = TreasuryManagerOperation::new(Operation::Deposit);
    let op_new_final = TreasuryManagerOperation::new_final(Operation::Deposit);

    // Both produce identical output due to the bug:
    assert_eq!(op_new.to_string(), op_new_final.to_string());
    // "TreasuryManager.Deposit-0" == "TreasuryManager.Deposit-0"
    // Expected: "TreasuryManager.Deposit-0-fin" for new_final

    // Ledger memo is also wrong:
    let memo: Vec<u8> = op_new_final.into();
    assert_eq!(memo, b"TreasuryManager.Deposit-0");
    // Expected: b"TreasuryManager.Deposit-0-fin"
}
``` [9](#0-8)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L311-325)
```rust
#[derive(CandidType, Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
pub struct Step {
    pub index: usize,
    pub is_final: bool,
}

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

**File:** rs/sns/treasury_manager/src/lib.rs (L352-371)
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
```

**File:** rs/sns/treasury_manager/src/lib.rs (L384-393)
```rust
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L229-253)
```text
// Example use case in the audit trail:
//
// ```candid
// transactions = vec {
//   record {
//     treasury_manager_operation = {
//       operation = Deposit;
//       step = record {
//         index = 0;
//         is_final = false;
//       };
//     };
//     ...
//   };
//   record {
//     treasury_manager_operation = {
//       operation = Deposit;
//       step = record {
//         index = 1;
//         is_final = true;
//       };
//     };
//     ...
//   };
// };
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L271-295)
```text
// Parties involved in the treasury asset management process:
// 1. treasury_owner     - e.g., the SNS Governance canister.
// 2. treasury_manager   - this canister.
// 3. external_custodian - e.g., the DEX in which assets are held temporarily.
// 4. fee_collector      - takes into account all the fees incurred due to treasury_manager's work.
// 5. payees             - e.g., developer salary payments.
// 6. payers             - e.g., liquidity provider rewards.
//
// Expects flow of assets:
//
// (A) Initialization / Deposit
// ============================
//                                      ,--------------> payees
//                                     /
// treasury_owner ---> treasury_manager ---> external_custodian
//              \                      \                       \
//               `----------------------`-----------------------`--------> fee_collector
//
// (B) Withdrawal
// ==============
//             payers --->.
//                         \
//  external_custodian ---> treasury_manager ---> treasury_owner
//                    \                     \
//                     `---------------------`---------------------------> fee_collector
```
