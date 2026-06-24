### Title
Single Hardcoded Authorized Caller Controls All CloudEngine Subnet Lifecycle Operations - (File: rs/engine_controller/tests/tests.rs)

### Summary
The Engine Controller canister (`ENGINE_CONTROLLER_CANISTER_ID`, `si2b5-pyaaa-aaaaa-aaaja-cai`) hardcodes a single authorized caller principal that has exclusive, ungoverned control over `create_engine` and `delete_engine` operations. This is a direct analog to the Malt Finance centralized-admin finding: one key controls critical protocol lifecycle operations with no DAO or multisig fallback, and loss or compromise of that key either freezes or fully compromises CloudEngine subnet management.

### Finding Description

The Engine Controller canister source code hardcodes a single authorized caller principal. The integration test file explicitly documents this with the comment:

> `// Must match the principal hard-coded in engine_controller.`
> `const AUTHORIZED_CALLER: &str = "bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe";` [1](#0-0) 

This single principal is the **only** entity authorized to call `create_engine` and `delete_engine` on the Engine Controller canister. These calls in turn invoke privileged registry mutations:

- `do_update_subnet` — the engine controller is explicitly allowed to mutate CloudEngine subnets in the registry, bypassing the normal NNS governance path. [2](#0-1) 

- `do_deploy_guestos_to_all_subnet_nodes` — the engine controller can deploy GuestOS to CloudEngine subnets, again bypassing governance. [3](#0-2) 

The registry canister's ingress authorization gate for these methods is `check_caller_is_governance_or_engine_controller_and_log`, meaning the engine controller canister itself is a co-equal privileged caller alongside NNS governance for these registry mutations. [4](#0-3) 

The hardcoded default is not merely a test artifact. The upgrade test explicitly shows that upgrading the canister with `None` args **resets** the authorized caller back to the hardcoded default principal, meaning the single-key dependency is baked into the canister's upgrade lifecycle: [5](#0-4) 

The Engine Controller canister is registered at index 18 in the NNS subnet: [6](#0-5) [7](#0-6) 

### Impact Explanation

**Scenario A — Key loss / unavailability:** If the single authorized caller's private key is lost, rotated without updating the canister, or the key holder becomes unavailable, `create_engine` and `delete_engine` are permanently frozen. No CloudEngine subnet can be created or deleted until NNS governance passes a proposal to upgrade the canister with a new authorized caller. This is a direct protocol liveness failure for the CloudEngine subnet lifecycle.

**Scenario B — Key compromise:** If the authorized caller's private key is stolen, the attacker gains ungoverned, unilateral ability to:
- Create arbitrary new CloudEngine subnets (registry pollution, resource exhaustion, DKG ceremony abuse).
- Delete existing CloudEngine subnets (irreversible service disruption for all workloads on those subnets).
- Update subnet configuration and deploy GuestOS versions to CloudEngine subnets via the registry co-authorization path.

All of these actions bypass NNS governance entirely. There is no on-chain multisig, no threshold requirement, and no time-lock.

### Likelihood Explanation

The hardcoded principal is a single self-authenticating key. Single-key operational principals are routinely rotated, lost in key management incidents, or targeted by attackers. The fact that the canister's upgrade path resets to this hardcoded default (rather than requiring an explicit governance proposal to set a new key) amplifies the risk: any accidental upgrade with `None` args silently re-centralizes control. Likelihood is **Medium** — not requiring active exploitation, just operational key management failure.

### Recommendation

1. Replace the single hardcoded authorized caller with NNS governance as the sole authorized caller for `create_engine` and `delete_engine`, routing these operations through the standard NNS proposal mechanism (as is done for all other registry mutations).
2. If a non-governance operational caller is required for latency reasons, replace the single principal with a threshold-signature-based multisig or an SNS/DAO-controlled canister, not a single self-authenticating key.
3. Remove the "upgrade with `None` resets to hardcoded default" behavior. Any change to the authorized caller should require an explicit, auditable governance action.

### Proof of Concept

**Entry path:** Any principal holding the private key for `bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe` sends an ingress `update_call` to `ENGINE_CONTROLLER_CANISTER_ID` (`si2b5-pyaaa-aaaaa-aaaja-cai`) calling `create_engine` or `delete_engine`.

**Root cause chain:**
1. Engine Controller canister checks `caller == hardcoded_authorized_caller` — passes for the key holder.
2. Engine Controller calls Registry canister `create_subnet` / `delete_subnet` (or `update_subnet` / `deploy_guestos_to_all_subnet_nodes`).
3. Registry canister's `check_caller_is_governance_or_engine_controller_and_log` passes because the caller is `ENGINE_CONTROLLER_CANISTER_ID`.
4. Registry mutation is committed — subnet created, deleted, or reconfigured — with no NNS governance proposal, no neuron vote, no time-lock. [8](#0-7) [4](#0-3)

### Citations

**File:** rs/engine_controller/tests/tests.rs (L37-38)
```rust
// Must match the principal hard-coded in `engine_controller`.
const AUTHORIZED_CALLER: &str = "bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe";
```

**File:** rs/engine_controller/tests/tests.rs (L285-296)
```rust
#[tokio::test]
async fn create_engine_caller_must_be_authorized() {
    let (pic, node_ids, _nns_subnet_id) = setup(4).await;
    let attacker = Principal::self_authenticating(b"attacker");
    let args = CreateEngineArgs {
        node_ids: node_principals(&node_ids),
        subnet_admins: vec![],
        replica_version_id: test_replica_version(),
    };
    let err = call_create_engine(&pic, attacker, &args).await.unwrap_err();
    assert!(err.contains("not authorized"), "unexpected error: {err}");
}
```

**File:** rs/engine_controller/tests/tests.rs (L416-433)
```rust
    // Upgrade with no override: default principal becomes authorized again.
    pic.upgrade_canister(
        ENGINE_CONTROLLER_CANISTER_ID.into(),
        wasm.bytes(),
        Encode!(&None::<EngineControllerInitArgs>).unwrap(),
        Some(Principal::anonymous()),
    )
    .await
    .expect("upgrade should succeed");

    let err = call_create_engine(&pic, custom_caller, &args)
        .await
        .unwrap_err();
    assert!(
        err.contains("not authorized"),
        "custom caller must be rejected after upgrade with default: {err}"
    );
}
```

**File:** rs/registry/canister/src/mutations/do_update_subnet.rs (L38-51)
```rust
        // The engine controller canister is only allowed to mutate CloudEngine
        // subnets, and only a small subset of fields. Other authorized callers
        // (governance) can update any subnet and any field.
        if caller == ENGINE_CONTROLLER_CANISTER_ID.get() {
            let subnet_record = self.get_subnet_or_panic(subnet_id);
            assert_eq!(
                subnet_record.subnet_type,
                i32::from(SubnetTypePb::CloudEngine),
                "{LOG_PREFIX}do_update_subnet: engine controller may only update CloudEngine \
                 subnets; subnet {subnet_id} has subnet_type {:?}",
                subnet_record.subnet_type,
            );
            ensure_engine_controller_payload_scope(&payload);
        }
```

**File:** rs/registry/canister/src/mutations/do_deploy_guestos_to_all_subnet_nodes.rs (L29-39)
```rust
        // subnets. Other authorized callers (governance) can update any subnet.
        if caller == ENGINE_CONTROLLER_CANISTER_ID.get() {
            let subnet_record = self.get_subnet_or_panic(subnet_id);
            assert_eq!(
                subnet_record.subnet_type,
                i32::from(SubnetTypePb::CloudEngine),
                "{LOG_PREFIX}do_deploy_guestos_to_all_subnet_nodes: engine controller may only \
                 deploy GuestOS to CloudEngine subnets; subnet {subnet_id} has subnet_type {:?}",
                subnet_record.subnet_type,
            );
        }
```

**File:** rs/registry/canister/canister/canister.rs (L137-144)
```rust
fn check_caller_is_governance_or_engine_controller_and_log(method_name: &str) {
    let caller = dfn_core::api::caller();
    println!("{LOG_PREFIX}call: {method_name} from: {caller}");
    assert!(
        caller == GOVERNANCE_CANISTER_ID.into() || caller == ENGINE_CONTROLLER_CANISTER_ID.into(),
        "{LOG_PREFIX}Principal: {caller} is not authorized to call this method: {method_name}"
    );
}
```

**File:** rs/nns/constants/src/lib.rs (L36-36)
```rust
pub const ENGINE_CONTROLLER_CANISTER_INDEX_IN_NNS_SUBNET: u64 = 18;
```

**File:** rs/nns/constants/src/lib.rs (L141-143)
```rust
/// 18: si2b5-pyaaa-aaaaa-aaaja-cai
pub const ENGINE_CONTROLLER_CANISTER_ID: CanisterId =
    CanisterId::from_u64(ENGINE_CONTROLLER_CANISTER_INDEX_IN_NNS_SUBNET);
```
