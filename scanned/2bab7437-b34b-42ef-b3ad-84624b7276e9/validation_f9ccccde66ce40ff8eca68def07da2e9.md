The code discrepancy is real and concrete. Let me lay out the exact findings.

**`ValidatedDepositOperationArg::try_from`** only checks for the presence of the two required keys, with no map-length guard: [1](#0-0) 

**`construct_deposit_allowances`** enforces `map.len() != 2` strictly: [2](#0-1) 

The execution path is: `execute_treasury_manager_deposit` → `construct_treasury_manager_deposit_payload` → `construct_treasury_manager_deposit_allowances` → `construct_deposit_allowances`. [3](#0-2) [4](#0-3) 

The `original` field (which carries the full, unmodified `Precise` value including extra fields) is what gets passed all the way to `construct_deposit_allowances`: [5](#0-4) 

---

### Title
Validation/Execution Split on PreciseMap Length Causes Permanently-Failing Deposit Proposals — (`rs/sns/governance/src/extensions.rs`)

### Summary
`ValidatedDepositOperationArg::try_from` accepts any `PreciseMap` that contains the two required keys regardless of total map size, but `construct_deposit_allowances` (called only at execution time) rejects any map whose length is not exactly 2. A proposal carrying a 3-field map passes proposal validation and voting, then permanently fails at execution.

### Finding Description
`ValidatedDepositOperationArg::try_from` (lines 1674–1709) validates a `PreciseMap` by checking only that `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` are present and are `Nat` values. It does not enforce `map.len() == 2`. The validated struct stores the full `original: Precise` value.

At execution time, `execute_treasury_manager_deposit` destructures the struct and passes `original` to `construct_treasury_manager_deposit_payload`, which calls `construct_treasury_manager_deposit_allowances`, which calls `treasury_manager::construct_deposit_allowances`. That function checks:

```rust
if map.len() != 2 {
    return Err(format!(
        "{PREFIX}Top-level type must be PreciseMap with exactly 2 entries."
    ));
}
```

Any `original` with more than 2 entries will always fail here. Because the proposal has already passed voting, the failure is permanent — the proposal is marked failed and cannot be retried.

### Impact Explanation
Any neuron holder with enough stake to submit proposals can submit an `ExecuteExtensionOperation` deposit proposal with a 3-field map. The proposal passes `validate_deposit_operation_impl`, passes voting, and then permanently fails at execution. The governance cycle (neuron voting, wait-for-quiet, etc.) is consumed for a proposal that can never succeed. Repeated submissions waste governance bandwidth and can crowd out legitimate treasury deposit proposals. The `original` field is also used for the `RegisterExtension` init path via `construct_treasury_manager_init_payload`, so the same split affects extension registration.

### Likelihood Explanation
The attacker only needs a neuron with proposal-submission stake — a normal SNS participant. The exploit is trivially constructable (add one extra key to the map). There is no randomness or timing dependency. The discrepancy is stable across upgrades unless the validation is fixed.

### Recommendation
Add a map-length check to `ValidatedDepositOperationArg::try_from` immediately after extracting the map reference:

```rust
if map.len() != 2 {
    return Err(format!(
        "Deposit operation arguments must be a PreciseMap with exactly 2 entries, got {}",
        map.len()
    ));
}
```

This makes validation complete and consistent with `construct_deposit_allowances`, satisfying the invariant that any value passing `try_from` also passes execution-time parsing.

### Proof of Concept
```rust
let three_field_arg = Some(Precise {
    value: Some(precise::Value::Map(PreciseMap {
        map: btreemap! {
            "treasury_allocation_sns_e8s".to_string() => Precise {
                value: Some(precise::Value::Nat(1_000_000)),
            },
            "treasury_allocation_icp_e8s".to_string() => Precise {
                value: Some(precise::Value::Nat(2_000_000)),
            },
            "extra_field".to_string() => Precise {
                value: Some(precise::Value::Text("z".to_string())),
            },
        },
    })),
});

// Passes validation — no map.len() check
let validated = ValidatedDepositOperationArg::try_from(three_field_arg).unwrap();

// Fails at execution — map.len() == 3 != 2
let result = treasury_manager::construct_deposit_allowances(
    validated.original,
    dummy_sns_asset(),
    dummy_icp_asset(),
    dummy_sns_account(),
    dummy_icp_account(),
);
assert!(result.is_err()); // "exactly 2 entries"
```

### Citations

**File:** rs/sns/governance/src/extensions.rs (L883-887)
```rust
        if map.len() != 2 {
            return Err(format!(
                "{PREFIX}Top-level type must be PreciseMap with exactly 2 entries."
            ));
        }
```

**File:** rs/sns/governance/src/extensions.rs (L1088-1099)
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;

    let arg = DepositRequest { allowances };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding DepositRequest: {err}"))?;

    Ok(arg)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1551-1555)
```rust
    let ValidatedDepositOperationArg {
        treasury_allocation_sns_e8s,
        treasury_allocation_icp_e8s,
        original,
    } = arg;
```

**File:** rs/sns/governance/src/extensions.rs (L1558-1564)
```rust
    let arg_blob =
        construct_treasury_manager_deposit_payload(context, original).map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Failed to construct treasury manager deposit payload: {err}"),
            )
        })?;
```

**File:** rs/sns/governance/src/extensions.rs (L1682-1708)
```rust
        let map = match &original.value {
            Some(precise::Value::Map(PreciseMap { map })) => map,
            _ => return Err("Deposit operation arguments must be a PreciseMap".to_string()),
        };

        let treasury_allocation_sns_e8s = map
            .get("treasury_allocation_sns_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_sns_e8s must be a Nat value".to_string())?;

        let treasury_allocation_icp_e8s = map
            .get("treasury_allocation_icp_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_icp_e8s must be a Nat value".to_string())?;

        Ok(Self {
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
            original,
        })
    }
```
