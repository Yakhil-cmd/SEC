### Title
Rented Subnet Canister-Creation Restriction Bypassed via Same-Subnet Factory Canister - (File: rs/execution_environment/src/canister_manager.rs)

### Summary

On rented subnets (application subnets with `CanisterCyclesCostSchedule::Free` and `subnet_admins` configured), the `create_canister` management call enforces the subnet-admin restriction only for **ingress messages**. When the sender is a canister already resident on the same subnet, the subnet-admin check is unconditionally skipped. A subnet admin who deploys any canister that exposes a public canister-creation endpoint (a "factory canister") allows any unprivileged user to create canisters on the rented subnet, defeating the isolation guarantee that is the entire purpose of the rented-subnet feature.

### Finding Description

The `should_accept_ingress_message` function in `CanisterManager` enforces that `CreateCanister` via ingress is only allowed by subnet admins when `subnet_admins` are configured: [1](#0-0) 

However, the actual execution path for canister-to-canister `create_canister` calls goes through `CanisterManager::create_canister`, which contains the following logic: [2](#0-1) 

The comment on line 754 is explicit: **"canisters on NNS or the same subnet can always create canisters."** When `sender_subnet_is_nns_or_self` returns `Ok(())` (i.e., the calling canister lives on the same subnet), the entire subnet-admin validation block is bypassed. The `validate_subnet_admin` call is only reached when the sender is on a *different* subnet. [3](#0-2) 

The `get_own_subnet_admins` / `can_have_subnet_admins` machinery correctly identifies rented subnets: [4](#0-3) [5](#0-4) 

But this check is never consulted for same-subnet canister senders in `create_canister`.

The rented-subnet design intent is unambiguous — the system test explicitly states: [6](#0-5) 

Yet the test only verifies the ingress path. The canister-to-canister path is untested and unguarded.

The existing unit test `create_canister_free` (which uses a free-cost-schedule subnet) demonstrates that a canister on such a subnet can successfully call `create_canister` without any subnet-admin check: [7](#0-6) 

### Impact Explanation

On a rented subnet, the subnet admin (the renter) is the only principal who should be able to create canisters. If the subnet admin deploys any canister that exposes a public update method that internally calls `ic00::create_canister` — intentionally as a factory, or inadvertently as part of normal application logic — any unprivileged user can invoke that method to create canisters on the rented subnet. This:

- Violates the isolation guarantee of the rented-subnet product (the renter pays for exclusive use of the subnet's resources).
- Allows unauthorized principals to consume the subnet's canister-ID namespace and resources.
- Undermines the business model of subnet rental, since the renter cannot prevent third parties from deploying on their subnet once any canister with creation capability exists.

### Likelihood Explanation

The likelihood is **medium**. The attack requires the subnet admin to have deployed at least one canister that exposes canister-creation functionality. This is a realistic scenario because:

1. The subnet admin's own application canisters may legitimately need to spawn sub-canisters (e.g., a multi-tenant SaaS canister that creates per-user canisters).
2. Any such canister becomes a publicly accessible factory unless the canister itself implements its own access control — which is an easy mistake to omit.
3. The IC's canister model makes canister-to-canister `create_canister` calls a standard pattern, so developers may not realize they are creating a bypass.

The attacker entry path is: unprivileged ingress sender → calls a publicly accessible update method on any factory canister on the rented subnet → factory canister calls `ic00::create_canister` → management canister's `create_canister` skips subnet-admin check because sender subnet == own subnet → new canister created.

### Recommendation

In `CanisterManager::create_canister`, when `subnet_admins` are configured (i.e., `get_own_subnet_admins()` returns `Some`), apply the subnet-admin check to **all** callers — including same-subnet canister callers — not only to cross-subnet callers. The current structure should be changed so that the `sender_subnet_is_nns_or_self` fast-path is gated on the absence of subnet admins:

```rust
let subnet_admins = state.get_own_subnet_admins();
if let Some(ref admins) = subnet_admins {
    // Rented subnet: always enforce subnet-admin restriction,
    // regardless of whether the sender is on the same subnet.
    if let Err(err) = validate_subnet_admin(admins, &sender) {
        return (Err(err.into()), cycles);
    }
} else {
    // Non-rented subnet: only reject cross-subnet senders.
    if let Err(err) = self.sender_subnet_is_nns_or_self(state, &sender) {
        return (Err(err.into()), cycles);
    }
}
```

### Proof of Concept

1. Configure a rented subnet (application subnet, `CanisterCyclesCostSchedule::Free`, `subnet_admins = {ADMIN}`).
2. As `ADMIN`, deploy a universal canister `factory` on the rented subnet (succeeds via ingress because `ADMIN` is a subnet admin).
3. As any unprivileged user `EVE`, send an ingress update to `factory` with a payload that calls `ic00::create_canister` with attached cycles.
4. `factory` (a same-subnet canister) calls `CanisterManager::create_canister`; `sender_subnet_is_nns_or_self` returns `Ok(())` because `factory` is on the same subnet; the subnet-admin check is skipped.
5. A new canister is created on the rented subnet under `EVE`'s control, despite `EVE` not being a subnet admin.

The existing test `create_canister_free` at line 7396 already demonstrates step 4 succeeds on a free-cost-schedule subnet without subnet-admin validation. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/execution_environment/src/canister_manager.rs (L171-181)
```rust
            // Canister creation via ingress is only allowed by subnet admins.
            Ok(Ic00Method::CreateCanister) => {
                let subnet_admins = state.get_own_subnet_admins();
                if let Some(subnet_admins) = subnet_admins {
                  validate_subnet_admin(&subnet_admins, sender.get_ref()).map_err(|err| err.into())
                } else {
                  // In case the subnet admins are not set, return the same error as
                  // before introducing the notion of subnet admins to maintain backward compatibility.
                  Err(UserError::new(ErrorCode::CanisterRejectedMessage, format!("Only canisters can call ic00 method {method_name}")))
                }
            }
```

**File:** rs/execution_environment/src/canister_manager.rs (L723-736)
```rust
    /// Check if the sender is on NNS or on the same subnet.
    fn sender_subnet_is_nns_or_self(
        &self,
        state: &ReplicatedState,
        sender: &PrincipalId,
    ) -> Result<(), UserError> {
        let sender_subnet_id = state.find_subnet_id(*sender)?;
        if sender_subnet_id != state.metadata.network_topology.nns_subnet_id
            && sender_subnet_id != self.config.own_subnet_id
        {
            return Err(CanisterManagerError::InvalidSenderSubnet(sender_subnet_id).into());
        }
        Ok(())
    }
```

**File:** rs/execution_environment/src/canister_manager.rs (L752-774)
```rust
        let sender = origin.origin();
        match self.sender_subnet_is_nns_or_self(state, &sender) {
            // canisters on NNS or the same subnet can always create canisters
            Ok(()) => (),
            Err(sender_subnet_err) => {
                let subnet_admins = state.get_own_subnet_admins();
                if let Some(subnet_admins) = subnet_admins {
                    if let Err(err) = validate_subnet_admin(&subnet_admins, &sender) {
                        return (Err(err.into()), cycles);
                    }
                } else {
                    // if subnet admins are not set, then the sender must be a canister
                    // and the canister creation message should not have been routed
                    // all the way to here unless the sender subnet is buggy/malicious
                    canister_creation_error.inc();
                    error!(
                        self.log,
                        "[EXC-BUG] Misrouted canister creation request from sender {}", sender
                    );
                    return (Err(sender_subnet_err), cycles);
                }
            }
        };
```

**File:** rs/replicated_state/src/replicated_state.rs (L781-790)
```rust
    /// Returns the list of subnet admins of this subnet.
    pub fn get_own_subnet_admins(&self) -> Option<BTreeSet<PrincipalId>> {
        let subnet_id = self.metadata.own_subnet_id;
        let subnet_topology = self.metadata.network_topology.subnets().get(&subnet_id)?;
        if can_have_subnet_admins(subnet_topology.subnet_type, subnet_topology.cost_schedule) {
            Some(subnet_topology.subnet_admins.clone())
        } else {
            None
        }
    }
```

**File:** rs/replicated_state/src/metadata_state.rs (L401-411)
```rust
/// Only rented subnets, i.e., application subnets on a "free" cost schedule,
/// and cloud engines on a "free" cost schedule can have subnet admins set.
#[allow(clippy::nonminimal_bool)]
pub fn can_have_subnet_admins(
    subnet_type: SubnetType,
    cost_schedule: CanisterCyclesCostSchedule,
) -> bool {
    (subnet_type == SubnetType::Application && cost_schedule == CanisterCyclesCostSchedule::Free)
        || (subnet_type == SubnetType::CloudEngine
            && cost_schedule == CanisterCyclesCostSchedule::Free)
}
```

**File:** rs/tests/nns/rent_subnet_test.rs (L11-19)
```rust
. User creates a canister. It lands in the rented subnet. The canister can serve requests, but crucially, is not charged cycles. It can run on 0 cycles.
. Another principal (i.e. besides the user) tries to create a canister in the rented subnet, but they are not allowed to do that.

Success::
. Proposals execute successfully.
. Rented subnet gets created.
. User principal can create canisters in the rented subnet.
. The user's canisters can serve traffic.
. Other users CANNOT create canisters in the rented subnet.
```

**File:** rs/execution_environment/src/canister_manager/tests.rs (L7396-7442)
```rust
#[test]
fn create_canister_free() {
    let cost_schedule = CanisterCyclesCostSchedule::Free;
    let mut test = ExecutionTestBuilder::new()
        .with_cost_schedule(cost_schedule)
        .build();

    let canister_id = test
        .universal_canister_with_cycles(*INITIAL_CYCLES * 2_u64)
        .unwrap();

    let create_canister_args = CreateCanisterArgs {
        settings: None,
        sender_canister_version: None,
    }
    .encode();
    let payload = wasm()
        .call_with_cycles(
            CanisterId::ic_00(),
            Method::CreateCanister,
            call_args()
                .other_side(create_canister_args)
                .on_reply(wasm().message_payload().append_and_reply()),
            *INITIAL_CYCLES,
        )
        .build();

    test.ingress(canister_id, "update", payload).unwrap();

    let creation_fee = test
        .cycles_account_manager()
        .canister_creation_fee(test.get_own_subnet_cycles_config())
        .real();
    assert_eq!(creation_fee, Cycles::new(0));
    // There's only 2 canisters on the subnet, so the one created from the first one
    // with have the test id corresponding to `1`.
    let canister = test.canister_state(canister_test_id(1));
    assert_eq!(
        canister
            .system_state
            .canister_metrics()
            .consumed_cycles()
            .get(),
        CANISTER_CREATION_FEE.get()
    );
    assert_eq!(canister.system_state.balance(), *INITIAL_CYCLES);
}
```
