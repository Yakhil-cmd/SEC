### Title
SNS Governance Uses Stale Maturity Modulation Without Freshness Check at Disbursement - (`rs/sns/governance/src/governance.rs`)

### Summary

`effective_maturity_modulation_basis_points` in SNS governance only checks whether `current_basis_points` is `Some`, but never validates whether the stored value is fresh by inspecting `updated_at_timestamp_seconds`. This is the direct IC analog of the BendDAO oracle bug: the code stores a timestamp alongside the price-like value but fails to use it as a staleness guard at the point of financial use.

### Finding Description

`GovernanceProto::effective_maturity_modulation_basis_points` is the function that supplies the maturity-to-ICP conversion factor at disbursement time:

```rust
fn effective_maturity_modulation_basis_points(&self) -> Result<i32, GovernanceError> {
    // ...
    self.maturity_modulation
        .as_ref()
        .and_then(|maturity_modulation| maturity_modulation.current_basis_points)
        .ok_or_else(|| GovernanceError::new_with_message(
            ErrorType::Unavailable,
            "Maturity modulation not known. ...",
        ))
}
``` [1](#0-0) 

The `MaturityModulation` proto message carries both `current_basis_points` and `updated_at_timestamp_seconds`: [2](#0-1) 

The function checks only that `current_basis_points` is `Some` — the equivalent of `updatedAt != 0` in the BendDAO bug — and completely ignores `updated_at_timestamp_seconds`. There is no guard of the form `now - updated_at_timestamp_seconds < MAX_STALENESS`.

The refresh logic in `should_update_maturity_modulation` does compare age against `ONE_DAY_SECONDS` to decide whether to *fetch* a new value: [3](#0-2) 

But if the CMC call fails (network partition, canister trap, rate-limit), `update_maturity_modulation` silently returns without updating the stored value: [4](#0-3) 

The stale value is then consumed unconditionally by `maybe_finalize_disburse_maturity`: [5](#0-4) 

### Impact Explanation

Maturity modulation is the multiplier applied when converting neuron maturity to ICP tokens. A stale value — potentially days or weeks old — is applied to every pending disbursement that matures during the outage window. Because the modulation can range from −5 % to +5 % of the disbursed amount, users receive systematically incorrect ICP amounts. The error compounds across all neurons whose `finalize_disbursement_timestamp_seconds` falls within the stale window. Unlike a simple read, this triggers an irreversible ledger mint.

### Likelihood Explanation

The CMC (`rkp4c-7iaaa-aaaaa-aaaca-cai`) is a system canister on the NNS subnet. Transient unavailability (upgrade, subnet slowdown, inter-subnet message backlog) is a realistic operational event. The SNS governance canister silently continues using the last-known value with no expiry, no warning to callers, and no block on disbursement. Any neuron owner whose disbursement window closes during such a period is affected without any action on their part.

### Recommendation

Add a maximum-staleness guard inside `effective_maturity_modulation_basis_points`. Before returning the cached value, compare `updated_at_timestamp_seconds` against the current time and return `Err(Unavailable)` if the value is older than an acceptable threshold (e.g., 2–3 days):

```rust
fn effective_maturity_modulation_basis_points(&self) -> Result<i32, GovernanceError> {
    // ... disabled check ...
    let mm = self.maturity_modulation.as_ref()
        .ok_or_else(|| GovernanceError::new_with_message(ErrorType::Unavailable, "..."))?;

    // NEW: staleness guard
    if let Some(updated_at) = mm.updated_at_timestamp_seconds {
        let age = self.env.now().saturating_sub(updated_at);
        if age > MAX_MATURITY_MODULATION_STALENESS_SECONDS {
            return Err(GovernanceError::new_with_message(
                ErrorType::Unavailable,
                "Maturity modulation is stale; retrying later.",
            ));
        }
    }

    mm.current_basis_points.ok_or_else(|| ...)
}
```

### Proof of Concept

1. SNS governance canister is live; `maturity_modulation.updated_at_timestamp_seconds` is set to `T`.
2. CMC becomes transiently unreachable. `update_maturity_modulation` fires daily but returns early on every `Err` from `self.cmc.neuron_maturity_modulation()`.
3. After `MATURITY_DISBURSEMENT_DELAY_SECONDS` (7 days), `maybe_finalize_disburse_maturity` runs.
4. `effective_maturity_modulation_basis_points` returns the value stored at time `T` — now 7+ days stale — with no error.
5. `apply_maturity_modulation` mints ICP using the stale factor, producing an incorrect token amount for every neuron whose disbursement window closed during the outage. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L402-428)
```rust
    fn effective_maturity_modulation_basis_points(&self) -> Result<i32, GovernanceError> {
        let maturity_modulation_disabled = self
            .parameters
            .as_ref()
            .map(|nervous_system_parameters| {
                nervous_system_parameters
                    .maturity_modulation_disabled
                    .unwrap_or_default()
            })
            .unwrap_or_default();

        if maturity_modulation_disabled {
            return Ok(0);
        }

        self.maturity_modulation
            .as_ref()
            .and_then(|maturity_modulation| maturity_modulation.current_basis_points)
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::Unavailable,
                    "Maturity modulation not known. Retrying later might work. \
                     If this persists, there is probably a problem with retrieving \
                     the maturity modulation value from the Cycles Minting Canister.",
                )
            })
    }
```

**File:** rs/sns/governance/src/governance.rs (L4920-4933)
```rust
    // Disburses any maturity that should be disbursed, unless this is already happening.
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
        }

        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };
```

**File:** rs/sns/governance/src/governance.rs (L5677-5688)
```rust
    fn should_update_maturity_modulation(&self) -> bool {
        // Check if we're already updating the neuron maturity modulation.
        let updated_at_timestamp_seconds = self
            .proto
            .maturity_modulation
            .as_ref()
            .and_then(|maturity_modulation| maturity_modulation.updated_at_timestamp_seconds)
            .unwrap_or_default();

        let age_seconds = self.env.now() - updated_at_timestamp_seconds;
        age_seconds >= ONE_DAY_SECONDS
    }
```

**File:** rs/sns/governance/src/governance.rs (L5690-5717)
```rust
    async fn update_maturity_modulation(&mut self) {
        if !self.should_update_maturity_modulation() {
            return;
        };

        // Fetch new maturity modulation.
        let maturity_modulation = self.cmc.neuron_maturity_modulation().await;

        // Unwrap response.
        let Ok(maturity_modulation) = maturity_modulation else {
            return;
        };

        // Construct new MaturityModulation.
        let new_maturity_modulation = MaturityModulation {
            current_basis_points: Some(maturity_modulation),
            updated_at_timestamp_seconds: Some(self.env.now()),
        };
        println!(
            "{}Updating maturity modulation to {:#?}. Previously: {:#?}",
            log_prefix(),
            new_maturity_modulation,
            self.proto.maturity_modulation
        );

        // Store the new value.
        self.proto.maturity_modulation = Some(new_maturity_modulation);
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1675-1687)
```text
  message MaturityModulation {
    // When X maturity is disbursed, the amount that goes to the destination
    // account is X * (1 + y) where y = current_basis_points / 10_000.
    //
    // Fetched from the cycles minting canister (same as NNS governance).
    //
    // There is a positive relationship between the price of ICP (in XDR) and
    // this value.
    optional int32 current_basis_points = 1;

    // When current_basis_points was last updated (seconds since UNIX epoch).
    optional uint64 updated_at_timestamp_seconds = 2;
  }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L2351-2364)
```rust
    pub struct MaturityModulation {
        /// When X maturity is disbursed, the amount that goes to the destination
        /// account is X * (1 + y) where y = current_basis_points / 10_000.
        ///
        /// Fetched from the cycles minting canister (same as NNS governance).
        ///
        /// There is a positive relationship between the price of ICP (in XDR) and
        /// this value.
        #[prost(int32, optional, tag = "1")]
        pub current_basis_points: ::core::option::Option<i32>,
        /// When current_basis_points was last updated (seconds since UNIX epoch).
        #[prost(uint64, optional, tag = "2")]
        pub updated_at_timestamp_seconds: ::core::option::Option<u64>,
    }
```
