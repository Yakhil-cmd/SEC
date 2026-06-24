### Title
SNS-WASM Archive WASM Not Propagated to Ledger During SNS Deployment, Preventing Archive Compatibility After Ledger Upgrades - (File: rs/nns/sns-wasm/src/sns_wasm.rs)

### Summary
When SNS-WASM deploys a new SNS instance, the archive WASM stored in SNS-WASM's version registry is verified to exist but deliberately excluded from the deployment payload sent to the ledger canister. The ledger spawns archive canisters using its own embedded WASM, not the one registered in SNS-WASM. This mirrors the HyperdriveFactory bug: the "factory" (SNS-WASM) is responsible for tracking the archive WASM version, but the "deployer" (ledger) spawns archives independently using its own embedded code, making it impossible to change the archive WASM used by newly spawned archives without also upgrading the ledger itself.

### Finding Description
The `SnsWasmsForDeploy` struct in `rs/nns/sns-wasm/src/sns_wasm.rs` enumerates every WASM that SNS-WASM installs during SNS deployment — root, governance, ledger, swap, and index — but explicitly omits the archive WASM:

```rust
struct SnsWasmsForDeploy {
    root: Vec<u8>,
    governance: Vec<u8>,
    ledger: Vec<u8>,
    swap: Vec<u8>,
    index: Vec<u8>,
}
```

The `get_latest_version_wasms()` function reads the archive WASM from storage to confirm it exists, but does not include it in the returned struct. The in-code comment makes the design intent explicit:

```rust
// We do not need this to be set to install, but no upgrade path will be found by the installed
// SNS if we do not have this as part of the version.
self.read_wasm(
    &vec_to_hash(version.archive_wasm_hash.clone())
        .map_err(|_| "No archive wasm set for this version.".to_string())?,
)
.ok_or_else(|| "Archive wasm for this version not found in storage.".to_string())?;
```

The archive WASM is required to be present in SNS-WASM's registry so that the SNS upgrade path can reference it, but it is never forwarded to the ledger canister during initial SNS deployment. The ledger canister spawns archive canisters using its own internally embedded archive WASM binary, not the one tracked by SNS-WASM.

This creates a structural decoupling identical to the HyperdriveFactory pattern:

| HyperdriveFactory | SNS-WASM |
|---|---|
| Factory | SNS-WASM canister |
| Deployer (yield-source-specific) | Ledger canister |
| Data provider | Archive canister |
| Factory deploys data provider | Ledger spawns archive |
| Updating deployer doesn't update data provider | Updating archive WASM in SNS-WASM doesn't affect archives spawned by ledger |

When NNS governance publishes a new archive WASM to SNS-WASM (e.g., to fix a bug or add a new interface), that WASM is recorded in the SNS-WASM version registry and used for upgrading *existing* archive canisters via the SNS upgrade path. However, any *new* archive canisters spawned by the ledger after this point will still use the archive WASM embedded in the ledger binary — which may be a different, older version.

### Impact Explanation
If a new SNS ledger version requires a structurally different archive WASM (e.g., changed Candid interface, new storage layout, or new method signatures), the archive canisters spawned by the upgraded ledger will use the old embedded archive WASM. This can cause:

- **Interface mismatch**: The ledger calls archive methods that do not exist in the old archive WASM, causing inter-canister call failures and halting transaction archiving.
- **Data integrity risk**: Blocks that should be archived are not archived because the ledger-archive communication fails, leading to ledger memory exhaustion or silent data loss.
- **Irrecoverability**: Once the ledger spawns an archive with the wrong WASM, the SNS governance upgrade path can upgrade that archive to the correct WASM, but any blocks that failed to archive during the incompatibility window are lost.

### Likelihood Explanation
This is triggered by any NNS governance proposal that updates the archive WASM in SNS-WASM to a version that is not backward-compatible with the currently deployed ledger's embedded archive WASM, or by any ledger upgrade that embeds a new archive WASM that is not yet registered in SNS-WASM. Both scenarios are realistic during normal protocol evolution. The SNS-WASM canister is explicitly designed to allow independent WASM updates per canister type via NNS proposals, making version skew between the SNS-WASM registry and the ledger's embedded archive WASM a foreseeable operational state.

### Recommendation
The archive WASM bytes should be passed to the ledger canister during SNS deployment via the `ArchiveOptions` field (specifically the archive WASM module bytes), so that the ledger uses the archive WASM from SNS-WASM's registry when spawning new archives. This ensures the archive WASM version is always consistent with the version tracked by SNS-WASM, and that updating the archive WASM in SNS-WASM takes effect for both existing archives (via upgrade path) and newly spawned archives (via ledger initialization).

Alternatively, the `SnsWasmsForDeploy` struct should include the archive WASM, and the ledger initialization arguments should be constructed to pass the archive WASM bytes through `ArchiveOptions`, removing the dependency on the ledger's embedded archive binary.

### Proof of Concept

1. NNS governance submits and executes a proposal to add a new archive WASM (v2) to SNS-WASM. The new archive WASM has a changed Candid interface required by the upcoming ledger v2.
2. NNS governance submits and executes a proposal to add a new ledger WASM (v2) to SNS-WASM. The new ledger WASM embeds the old archive WASM (v1) binary internally.
3. An SNS instance upgrades its ledger to v2 via `UpgradeSnsToNextVersion`. The ledger canister is now running v2.
4. Transaction volume causes the ledger v2 to spawn a new archive canister. The ledger uses its own embedded archive WASM (v1), not the v2 registered in SNS-WASM.
5. The ledger v2 attempts to call archive v1 using the new v2 interface. The call fails because archive v1 does not implement the new method.
6. Transaction archiving halts. The ledger accumulates blocks in memory. SNS-WASM's upgrade path can upgrade the archive to v2, but blocks that failed to archive during the incompatibility window are unrecoverable.

**Root cause lines**: [1](#0-0) [2](#0-1)

### Citations

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L117-123)
```rust
struct SnsWasmsForDeploy {
    root: Vec<u8>,
    governance: Vec<u8>,
    ledger: Vec<u8>,
    swap: Vec<u8>,
    index: Vec<u8>,
}
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L1729-1743)
```rust
        // We do not need this to be set to install, but no upgrade path will be found by the installed
        // SNS if we do not have this as part of the version.
        self.read_wasm(
            &vec_to_hash(version.archive_wasm_hash.clone())
                .map_err(|_| "No archive wasm set for this version.".to_string())?,
        )
        .ok_or_else(|| "Archive wasm for this version not found in storage.".to_string())?;

        Ok(SnsWasmsForDeploy {
            root,
            governance,
            ledger,
            swap,
            index,
        })
```
