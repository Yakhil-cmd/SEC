### Title
Missing Hash-Content Integrity Check in `InstallCode` Proposal Allows Wasm Substitution Attack — (`rs/nns/governance/src/proposals/install_code.rs`)

### Summary

The `InstallCode` proposal type stores `wasm_module` (raw bytes) and `wasm_module_hash` as independent, unvalidated fields. Governance never verifies that `wasm_module_hash == SHA256(wasm_module)` at any point — proposal submission, validation, or execution. Voters are shown only the hash (via `abridge()` and `SelfDescribingValue`), while execution installs the raw `wasm_module` bytes. A neuron holder can submit a proposal with `wasm_module_hash = SHA256(wasm_A)` and `wasm_module = wasm_B`, causing `wasm_B` to be installed on the root canister if the proposal passes.

### Finding Description

**Entry point:** Any ICP neuron holder can submit an `InstallCode` governance proposal targeting `ROOT_CANISTER_ID`.

**`validate()` does not check hash-content consistency:** [1](#0-0) 

`valid_wasm_module()` only asserts `wasm_module.is_some()`: [2](#0-1) 

There is no `SHA256(wasm_module) == wasm_module_hash` check anywhere in the NNS governance proposal flow. A grep for any such check across all of `rs/nns/governance/` returns zero matches.

**Execution uses `wasm_module` directly, not the hash:** [3](#0-2) 

**Voters only see the hash.** The `abridge()` method elides `wasm_module` but preserves `wasm_module_hash`: [4](#0-3) 

The `SelfDescribingValue` conversion similarly exposes only `wasm_module_hash` to the UI: [5](#0-4) 

**Lifeline executes whatever wasm bytes it receives** — it has no independent hash verification: [6](#0-5) 

### Impact Explanation

If a malicious proposal passes governance voting, `wasm_B` is installed on the NNS root canister despite voters having reviewed and approved `SHA256(wasm_A)`. Root canister compromise gives an attacker control over all NNS-controlled canisters (registry, governance, ledger, etc.).

### Likelihood Explanation

The attack requires a governance majority to vote yes on the proposal. In practice, DFINITY and large neuron holders independently verify wasm bytes before voting, which significantly limits exploitability. The attack is not self-executing — it depends on the community being deceived. However, the system's own design presents `wasm_module_hash` as the authoritative identifier of what will be installed, and the system fails to enforce this invariant at any layer. This is a protocol-level integrity failure, not merely a UI concern.

### Recommendation

In `validate()`, add:

```rust
if let (Some(hash), Some(wasm)) = (&self.wasm_module_hash, &self.wasm_module) {
    let computed = Sha256::hash(wasm);
    if computed.as_slice() != hash.as_slice() {
        return Err(invalid_proposal_error(
            "wasm_module_hash does not match SHA256(wasm_module)"
        ));
    }
}
```

This enforces the invariant at proposal submission time, before the proposal is stored or voted on.

### Proof of Concept

```rust
let wasm_a = vec![1, 2, 3];
let wasm_b = vec![0xde, 0xad, 0xbe, 0xef]; // malicious wasm

let install_code = InstallCode {
    canister_id: Some(ROOT_CANISTER_ID.get()),
    wasm_module: Some(wasm_b.clone()),           // actual bytes: wasm_B
    wasm_module_hash: Some(Sha256::hash(&wasm_a).to_vec()), // hash of wasm_A
    install_mode: Some(CanisterInstallMode::Upgrade as i32),
    arg: Some(vec![]),
    arg_hash: Some(Sha256::hash(&[]).to_vec()),
    skip_stopping_before_installing: None,
};

// validate() passes — no hash-content check
assert_eq!(install_code.validate(), Ok(()));

// payload sent to lifeline contains wasm_B, not wasm_A
let payload = install_code.payload().unwrap();
let decoded = Decode!(&payload, UpgradeRootProposalPayload).unwrap();
assert_eq!(decoded.wasm_module, wasm_b); // wasm_B installed, wasm_A hash shown to voters
``` [7](#0-6)

### Citations

**File:** rs/nns/governance/src/proposals/install_code.rs (L30-42)
```rust
    pub fn validate(&self) -> Result<(), GovernanceError> {
        let _ = self.valid_canister_id()?;
        let _ = self.valid_install_mode()?;
        let _ = self.valid_wasm_module()?;
        let _ = self.valid_arg()?;
        let _ = self.valid_topic()?;
        let _ = self.canister_and_function()?;

        // In the future, we could potentially validate the wasm module to see if it's a valid gzip
        // or a valid WASM.

        Ok(())
    }
```

**File:** rs/nns/governance/src/proposals/install_code.rs (L70-76)
```rust
    fn valid_wasm_module(&self) -> Result<&Vec<u8>, GovernanceError> {
        // We do not want to copy the (potentially large) wasm module when validating, so we return
        // a reference and let the caller clone it if needed.
        self.wasm_module
            .as_ref()
            .ok_or(invalid_proposal_error("Wasm module is required"))
    }
```

**File:** rs/nns/governance/src/proposals/install_code.rs (L86-108)
```rust
    pub fn abridge(&self) -> Self {
        let Self {
            canister_id,
            install_mode,
            wasm_module_hash,
            arg_hash,
            skip_stopping_before_installing,

            // Elided.
            wasm_module: _,
            arg: _,
        } = self;

        Self {
            canister_id: *canister_id,
            install_mode: *install_mode,
            wasm_module: None,
            wasm_module_hash: wasm_module_hash.clone(),
            arg: None,
            arg_hash: arg_hash.clone(),
            skip_stopping_before_installing: *skip_stopping_before_installing,
        }
    }
```

**File:** rs/nns/governance/src/proposals/install_code.rs (L115-126)
```rust
    fn payload_to_upgrade_root(&self) -> Result<Vec<u8>, GovernanceError> {
        let stop_upgrade_start = !self.skip_stopping_before_installing.unwrap_or(false);
        let wasm_module = self.valid_wasm_module()?.clone();
        let module_arg = self.arg.clone().unwrap_or_default();

        Encode!(&UpgradeRootProposalPayload {
            stop_upgrade_start,
            wasm_module,
            module_arg,
        })
        .map_err(|e| invalid_proposal_error(&format!("Failed to encode payload: {e}")))
    }
```

**File:** rs/nns/governance/src/proposals/install_code.rs (L203-228)
```rust
impl From<InstallCode> for SelfDescribingValue {
    fn from(value: InstallCode) -> Self {
        let InstallCode {
            canister_id,
            install_mode,
            wasm_module_hash,
            arg_hash,
            skip_stopping_before_installing,
            wasm_module: _,
            arg: _,
        } = value;

        let install_mode = install_mode.map(SelfDescribingProstEnum::<CanisterInstallMode>::new);

        ValueBuilder::new()
            .add_field("canister_id", canister_id)
            .add_field("install_mode", install_mode)
            .add_field("wasm_module_hash", wasm_module_hash)
            .add_field("arg_hash", arg_hash)
            .add_field(
                "skip_stopping_before_installing",
                skip_stopping_before_installing.unwrap_or_default(),
            )
            .build()
    }
}
```

**File:** rs/nns/governance/src/proposals/install_code.rs (L383-415)
```rust
    #[test]
    fn test_upgrade_root_protocol_canister() {
        let install_code = InstallCode {
            canister_id: Some(ROOT_CANISTER_ID.get()),
            wasm_module: Some(vec![1, 2, 3]),
            install_mode: Some(CanisterInstallMode::Upgrade as i32),
            arg: Some(vec![4, 5, 6]),
            skip_stopping_before_installing: None,
            wasm_module_hash: Some(Sha256::hash(&[1, 2, 3]).to_vec()),
            arg_hash: Some(Sha256::hash(&[4, 5, 6]).to_vec()),
        };

        assert_eq!(install_code.validate(), Ok(()));
        assert_eq!(
            install_code.valid_topic(),
            Ok(Topic::ProtocolCanisterManagement)
        );
        assert_eq!(
            install_code.canister_and_function(),
            Ok((LIFELINE_CANISTER_ID, "upgrade_root"))
        );
        assert!(install_code.allowed_when_resources_are_low());
        let decoded_payload =
            Decode!(&install_code.payload().unwrap(), UpgradeRootProposalPayload).unwrap();
        assert_eq!(
            decoded_payload,
            UpgradeRootProposalPayload {
                stop_upgrade_start: true,
                wasm_module: vec![1, 2, 3],
                module_arg: vec![4, 5, 6],
            }
        );
    }
```

**File:** rs/nns/handlers/lifeline/interface/src/lib.rs (L6-11)
```rust
#[derive(Clone, Eq, PartialEq, Hash, CandidType, Deserialize, Serialize)]
pub struct UpgradeRootProposal {
    pub wasm_module: Vec<u8>,
    pub module_arg: Vec<u8>,
    pub stop_upgrade_start: bool,
}
```
