### Title
Broken Canister ID Validation in SNS Governance Init Allows Anonymous Principal as Critical Canister Address - (File: rs/sns/governance/src/governance.rs)

### Summary
`ValidGovernanceProto::validate_canister_id_field` is self-documented as a no-op. It calls `CanisterId::try_from(principal_id)`, which — as explicitly documented in the IC codebase — **always returns `Ok(_)` regardless of input**. This means the SNS Governance canister can be initialized with `PrincipalId::new_anonymous()` (or any invalid principal) as `ledger_canister_id`, `root_canister_id`, or `swap_canister_id`, bypassing the intended constructor-level validation entirely.

### Finding Description
In `rs/sns/governance/src/governance.rs`, `ValidGovernanceProto::validate_canister_id_field` is the function responsible for verifying that the three critical canister ID fields in the SNS Governance init payload are valid:

```rust
fn validate_canister_id_field(name: &str, principal_id: PrincipalId) -> Result<(), String> {
    // TODO(NNS1-1992) – CanisterId::try_from always returns `Ok(_)` so this
    // check does nothing.
    match CanisterId::try_from(principal_id) {
        Ok(_) => Ok(()),
        Err(err) => Err(format!(...)),
    }
}
``` [1](#0-0) 

The comment is accurate. In `rs/types/base_types/src/canister_id.rs`, the `TryFrom<PrincipalId>` implementation is explicitly documented as lying:

```rust
/// Warning: This LIES: it does not return Err when the input is invalid. In
/// fact, this ALWAYS returns Ok.
impl TryFrom<PrincipalId> for CanisterId {
    type Error = CanisterIdError;
    fn try_from(principal_id: PrincipalId) -> Result<Self, Self::Error> {
        Ok(Self::unchecked_from_principal(principal_id))
    }
}
``` [2](#0-1) 

The correct validation function that actually checks opaque class, 10-byte length, and penultimate byte is `CanisterId::try_from_principal_id`, but it is not used here. [3](#0-2) 

The `TryFrom<GovernanceProto> for ValidGovernanceProto` calls `validate_canister_id_field` for all three critical fields:

```rust
Self::validate_canister_id_field("root", root_canister_id)?;
Self::validate_canister_id_field("ledger", ledger_canister_id)?;
Self::validate_canister_id_field("swap", swap_canister_id)?;
``` [4](#0-3) 

Because these checks are no-ops, the anonymous principal passes validation. This is confirmed by production integration test code that successfully builds a governance init payload with `PrincipalId::new_anonymous()` for all three fields and passes it through `ValidGovernanceProto::try_from`: [5](#0-4) 

The `canister_init_` function in the SNS Governance canister calls `ValidGovernanceProto::try_from(...).expect(...)` and then uses the resulting `ledger_canister_id` to construct a `LedgerCanister` client. If the anonymous principal was supplied, all subsequent ledger calls will be directed to the anonymous principal — not a real canister. [6](#0-5) 

### Impact Explanation
An SNS Governance canister initialized with the anonymous principal (or any non-canister principal) as `ledger_canister_id`, `root_canister_id`, or `swap_canister_id` will have all cross-canister calls to those addresses silently fail or trap. This permanently breaks:
- All neuron staking and disbursement (ledger calls fail)
- All SNS upgrade proposals (root canister calls fail)
- All swap finalization (swap canister calls fail)

SNS token holders lose the ability to interact with the SNS. The state is unrecoverable without a governance upgrade, which itself requires a functioning governance — a deadlock.

### Likelihood Explanation
The normal SNS deployment path through the SNS-W canister constructs the init payload from a validated `SnsInitPayload` with real canister IDs. However, the `canister_init` entry point itself imposes no caller restriction — anyone who installs the SNS Governance WASM directly controls the init args. The broken `validate_canister_id_field` is the last line of defense at the canister level, and it is a documented no-op (TODO NNS1-1992). The risk is realized whenever the SNS Governance canister is deployed outside the SNS-W flow, or if the SNS-W canister itself ever passes a zero/anonymous principal due to a bug in its own payload construction.

### Recommendation
Replace `CanisterId::try_from` with `CanisterId::try_from_principal_id` inside `validate_canister_id_field`, and add an explicit `is_anonymous()` check:

```rust
fn validate_canister_id_field(name: &str, principal_id: PrincipalId) -> Result<(), String> {
    if principal_id.is_anonymous() {
        return Err(format!("{name} canister ID must not be the anonymous principal."));
    }
    CanisterId::try_from_principal_id(principal_id).map(|_| ()).map_err(|err| {
        format!("Unable to convert {name} PrincipalId to CanisterId: {err:#?}")
    })
}
``` [7](#0-6) 

### Proof of Concept
The following is valid production code that passes `ValidGovernanceProto::try_from` without error, demonstrating the broken validation:

```rust
// From rs/sns/integration_tests/src/timers.rs
let governance = GovernanceCanisterInitPayloadBuilder::new()
    .with_root_canister_id(PrincipalId::new_anonymous())   // anonymous!
    .with_ledger_canister_id(PrincipalId::new_anonymous()) // anonymous!
    .with_swap_canister_id(PrincipalId::new_anonymous())   // anonymous!
    .build();
// ValidGovernanceProto::try_from(governance) succeeds — no error.
``` [5](#0-4) 

The root cause is `CanisterId::try_from` always returning `Ok`, making `validate_canister_id_field` unconditionally return `Ok(())` for any input including the anonymous principal. [1](#0-0)

### Citations

**File:** rs/sns/governance/src/governance.rs (L499-508)
```rust
    fn validate_canister_id_field(name: &str, principal_id: PrincipalId) -> Result<(), String> {
        // TODO(NNS1-1992) – CanisterId::try_from always returns `Ok(_)` so this
        // check does nothing.
        match CanisterId::try_from(principal_id) {
            Ok(_) => Ok(()),
            Err(err) => Err(format!(
                "Unable to convert {name} PrincipalId to CanisterId: {err:#?}",
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L525-527)
```rust
        Self::validate_canister_id_field("root", root_canister_id)?;
        Self::validate_canister_id_field("ledger", ledger_canister_id)?;
        Self::validate_canister_id_field("swap", swap_canister_id)?;
```

**File:** rs/types/base_types/src/canister_id.rs (L115-145)
```rust
    pub fn try_from_principal_id(principal_id: PrincipalId) -> Result<Self, CanisterIdError> {
        // Must be opaque.
        if principal_id.class() != Ok(PrincipalIdClass::Opaque) {
            return Err(CanisterIdError::InvalidPrincipalId(format!(
                "Principal ID {} is of class {:?} (not Opaque).",
                principal_id,
                principal_id.class(),
            )));
        }

        // Must be of length 10.
        let raw = principal_id.as_slice();
        if raw.len() != 10 {
            return Err(CanisterIdError::InvalidPrincipalId(format!(
                "Principal ID {} consists of {} bytes (not 10).",
                principal_id,
                raw.len(),
            )));
        }

        // Byte 8 (penultimate) must be 0x01.
        if raw[8] != 0x01 {
            return Err(CanisterIdError::InvalidPrincipalId(format!(
                "Byte 8 (9th) of Principal ID {} is not 0x01: {}",
                principal_id,
                hex::encode(raw),
            )));
        }

        Ok(CanisterId(principal_id))
    }
```

**File:** rs/types/base_types/src/canister_id.rs (L166-178)
```rust
/// Warning: This LIES: it does not return Err when the input is invalid. In
/// fact, this ALWAYS returns Ok.
///
/// We cannot simply "fix" this, because there are callers who rely on the
/// "always Ok (even when invalid)" behavior. (E.g. they might immediately call
/// unwrap, and assume that it never panics.)
impl TryFrom<PrincipalId> for CanisterId {
    type Error = CanisterIdError;

    fn try_from(principal_id: PrincipalId) -> Result<Self, Self::Error> {
        Ok(Self::unchecked_from_principal(principal_id))
    }
}
```

**File:** rs/sns/integration_tests/src/timers.rs (L59-73)
```rust
fn governance_init() -> Governance {
    let mut governance = GovernanceCanisterInitPayloadBuilder::new()
        .with_root_canister_id(PrincipalId::new_anonymous())
        .with_ledger_canister_id(PrincipalId::new_anonymous())
        .with_swap_canister_id(PrincipalId::new_anonymous())
        .with_ledger_canister_id(PrincipalId::new_anonymous())
        .build();

    governance.metrics = Some(GovernanceCachedMetrics {
        timestamp_seconds: u64::MAX, // Ensure that cached metrics are not attempted to be refreshed in tests.
        ..Default::default()
    });

    governance
}
```

**File:** rs/sns/governance/canister/canister.rs (L217-253)
```rust
fn canister_init_(init_payload: sns_gov_pb::Governance) {
    let init_payload = ValidGovernanceProto::try_from(init_payload).expect(
        "Cannot start canister, because the deserialized \
         GovernanceProto is invalid in some way",
    );

    log!(
        INFO,
        "canister_init_: Initializing with: {}",
        init_payload.summary(),
    );

    let ledger_canister_id = init_payload.ledger_canister_id();

    unsafe {
        assert!(
            GOVERNANCE.is_none(),
            "{}Trying to initialize an already-initialized governance canister!",
            log_prefix()
        );
        let governance = Governance::new(
            init_payload,
            Box::new(CanisterEnv::new()),
            Box::new(LedgerCanister::new(ledger_canister_id)),
            Box::new(IcpLedgerCanister::<CdkRuntime>::new(NNS_LEDGER_CANISTER_ID)),
            Box::new(CMCCanister::<CdkRuntime>::new()),
        );
        let governance = if cfg!(feature = "test") {
            governance.enable_test_features()
        } else {
            governance
        };
        GOVERNANCE = Some(governance);
    }

    init_timers();
}
```

**File:** rs/types/base_types/src/principal_id.rs (L353-355)
```rust
    pub fn is_anonymous(&self) -> bool {
        self.as_slice() == [PrincipalIdClass::Anonymous as u8]
    }
```
