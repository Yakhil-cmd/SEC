### Title
Chunked WASM Validation Disabled on Mainnet Allows Invalid SNS Upgrade Proposals to Bypass Pre-Vote Integrity Check - (File: `rs/sns/governance/src/types.rs`)

### Summary
The `validate_chunked_wasm` function in SNS governance contains a compile-time flag that unconditionally disables stored-chunk existence verification on mainnet. A TODO marker explicitly acknowledges this is unfinished production logic. As a result, any SNS neuron holder can submit an `UpgradeSnsControlledCanister` proposal referencing chunk hashes that do not exist in the declared store canister, and the proposal will pass the validation/submission phase, enter the community voting period, and only fail at execution time — after governance resources have been consumed.

### Finding Description
In `rs/sns/governance/src/types.rs`, the `validate_chunked_wasm` async function is responsible for verifying that all chunk hashes listed in a `ChunkedCanisterWasm` upgrade proposal are actually present in the specified store canister before the proposal is accepted into the voting queue.

The critical section reads:

```rust
// TODO[NNS1-3550]: Enable stored chunks validation on mainnet.
#[cfg(feature = "test")]
let validate_stored_chunks: bool = true;
#[cfg(not(feature = "test"))]
let validate_stored_chunks: bool = false;
if validate_stored_chunks {
    // ... calls management canister stored_chunks, diffs against chunk_hashes_list ...
}
``` [1](#0-0) 

The `validate_stored_chunks` flag is hardcoded to `false` in every non-test build via `#[cfg(not(feature = "test"))]`. The entire block that calls `ic_00::stored_chunks`, decodes the response, and computes the set difference between required and available chunks is dead code on mainnet.

The function is called from `Wasm::validate`, which is invoked during proposal submission validation: [2](#0-1) 

A companion dead constant reinforces the pattern — a payload size limit for `ExecuteGenericNervousSystemFunction` is declared but never enforced:

```rust
#[allow(dead_code)]
/// TODO Use to validate the size of the payload 70 KB
const PROPOSAL_EXECUTE_SNS_FUNCTION_PAYLOAD_BYTES_MAX: usize = 70000;
``` [3](#0-2) 

The test suite itself is gated behind `#[cfg(feature = "test")]` with the same TODO, confirming the validation path is intentionally skipped on mainnet: [4](#0-3) [5](#0-4) 

### Impact Explanation
An SNS neuron holder with sufficient voting power to submit proposals (or who can pay the proposal rejection fee) can craft an `UpgradeSnsControlledCanister` proposal whose `ChunkedCanisterWasm.chunk_hashes_list` references chunk hashes that do not exist in the declared `store_canister_id`. Because `validate_stored_chunks = false` on mainnet, the governance canister accepts the proposal without querying `ic_00::stored_chunks`. The proposal enters the full voting period. If it achieves a majority, execution is attempted and fails at the management canister level (chunks are missing). The SNS upgrade is aborted, but the governance cycle — neuron voting power, wait-for-quiet extension, reward distribution — has already been consumed. Repeated submissions constitute a low-cost griefing attack against SNS governance throughput.

### Likelihood Explanation
Any principal holding SNS neurons above the proposal submission threshold can trigger this. No privileged access, key material, or subnet-majority corruption is required. The entry path is a standard SNS governance `ManageNeuron { MakeProposal { UpgradeSnsControlledCanister { chunked_canister_wasm: Some(...) } } }` ingress call. The TODO tag and the `#[cfg]` guard make the disabled state explicit and stable across builds.

### Recommendation
Remove the `#[cfg(feature = "test")]` / `#[cfg(not(feature = "test"))]` guards and set `validate_stored_chunks = true` unconditionally, resolving `TODO[NNS1-3550]`. If the concern is the synchronous inter-canister call latency, switch the call to a best-effort message as the second TODO in the same block already suggests (`// TODO[NNS1-3550]: Switch this call to best-effort`). Until then, the pre-vote integrity guarantee for chunked WASM upgrades is absent on mainnet. [6](#0-5) 

### Proof of Concept
1. Obtain SNS neurons with sufficient stake to submit a proposal to a target SNS.
2. Do **not** upload any WASM chunks to any store canister.
3. Submit the following proposal via `ManageNeuron`:
   ```
   UpgradeSnsControlledCanister {
     canister_id: <target dapp canister>,
     new_canister_wasm: [],
     chunked_canister_wasm: Some(ChunkedCanisterWasm {
       wasm_module_hash: <any 32 bytes>,
       store_canister_id: <any canister id>,
       chunk_hashes_list: [<fabricated chunk hash 1>, <fabricated chunk hash 2>],
     }),
   }
   ```
4. Observe that the SNS governance canister accepts the proposal (HTTP 200, proposal ID returned) — `validate_chunked_wasm` returns `Ok(())` because `validate_stored_chunks = false`.
5. The proposal enters the voting period. If it reaches adoption, execution fails at `install_chunked_code` because the chunks are absent, but the governance round has been fully consumed.

### Citations

**File:** rs/sns/governance/src/types.rs (L72-75)
```rust
#[allow(dead_code)]
/// TODO Use to validate the size of the payload 70 KB (for executing
/// SNS functions that are not canister upgrades)
const PROPOSAL_EXECUTE_SNS_FUNCTION_PAYLOAD_BYTES_MAX: usize = 70000;
```

**File:** rs/sns/governance/src/types.rs (L2803-2858)
```rust
    // TODO[NNS1-3550]: Enable stored chunks validation on mainnet.
    #[cfg(feature = "test")]
    let validate_stored_chunks: bool = true;
    #[cfg(not(feature = "test"))]
    let validate_stored_chunks: bool = false;
    if validate_stored_chunks {
        // TODO[NNS1-3550]: Switch this call to best-effort.
        let stored_chunks_response = env
            .call_canister(CanisterId::ic_00(), "stored_chunks", arg)
            .await;

        let stored_chunks_response = match stored_chunks_response {
            Ok(stored_chunks_response) => stored_chunks_response,
            Err(err) => {
                let defect = format!("Cannot call stored_chunks for {store_canister_id}: {err:?}");
                defects.push(defect);
                return Err(defects);
            }
        };

        let stored_chunks_response = match Decode!(&stored_chunks_response, StoredChunksReply) {
            Ok(stored_chunks_response) => stored_chunks_response,
            Err(err) => {
                let defect = format!(
                    "Cannot decode response from calling stored_chunks for {store_canister_id}: {err}"
                );
                defects.push(defect);
                return Err(defects);
            }
        };

        // Finally, check that the expected chunks were successfully uploaded to the store canister.
        let available_chunks = stored_chunks_response
            .0
            .iter()
            .map(|chunk| format_full_hash(&chunk.hash))
            .collect::<BTreeSet<_>>();
        let required_chunks = chunk_hashes_list
            .iter()
            .map(|chunk| format_full_hash(chunk))
            .collect::<BTreeSet<_>>();

        let missing_chunks = required_chunks
            .difference(&available_chunks)
            .cloned()
            .collect::<Vec<_>>();
        if !missing_chunks.is_empty() {
            let defect = format!(
                "{} out of {} expected WASM chunks were not uploaded to the store canister: {}",
                missing_chunks.len(),
                required_chunks.len(),
                missing_chunks.join(", ")
            );
            defects.push(defect);
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L2869-2884)
```rust
    pub async fn validate(
        &self,
        env: &dyn Environment,
        canister_upgrade_arg: &Option<Vec<u8>>,
    ) -> Result<(), Vec<String>> {
        match self {
            Self::Bytes(bytes) => validate_wasm_bytes(bytes, canister_upgrade_arg),
            Self::Chunked {
                wasm_module_hash,
                store_canister_id,
                chunk_hashes_list,
            } => {
                validate_chunked_wasm(env, wasm_module_hash, *store_canister_id, chunk_hashes_list)
                    .await
            }
        }
```

**File:** rs/sns/governance/src/types/tests.rs (L1313-1314)
```rust
// TODO[NNS1-3550]: Enable stored chunks validation on mainnet.
#[cfg(feature = "test")]
```
