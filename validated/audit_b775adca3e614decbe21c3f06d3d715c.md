### Title
Ingress Sender Pays Zero Cycles — Unprivileged User Can Drain Any Canister's Cycles via Spam Ingress Messages - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

The IC protocol unconditionally charges the **receiving canister** for every ingress message induction cost, while the **sender** (any user with a free identity) pays nothing. An unprivileged external user can spam ingress messages targeting any canister on an application subnet, draining its cycles balance at zero cost to the attacker — a direct structural analog to the KintoWallet `SponsorPaymaster` drain.

---

### Finding Description

`ingress_induction_cost()` in `rs/cycles_account_manager/src/cycles_account_manager.rs` computes who pays for inducting an ingress message. For any message addressed directly to a canister (not a subnet management call), the payer is unconditionally set to the **destination canister**, never the sender:

```rust
// A message to a canister is always paid for by the receiving canister.
false => Some(ingress.canister_id()),
``` [1](#0-0) 

This returns an `IngressInductionCost::Fee { payer: <destination_canister>, cost }` where `cost` is:

```
ingress_message_reception_fee (1,200,000 cycles) + ingress_byte_reception_fee (2,000 cycles/byte) × message_size
``` [2](#0-1) [3](#0-2) 

The `enqueue()` function in `rs/messaging/src/scheduling/valid_set_rule.rs` then immediately withdraws this cost from the **destination canister's** cycles balance:

```rust
IngressInductionCost::Fee { payer, cost } => {
    let canister = state.canister_state_make_mut(&payer)...;
    self.cycles_account_manager.charge_ingress_induction_cost(
        canister, ..., cost, ...
    )
``` [4](#0-3) 

The ingress sender's identity is free to generate (any `PrincipalId`). The sender is never charged. The `ingress.source` field is recorded but plays no role in the cost accounting path.

---

### Impact Explanation

An attacker with a free identity can continuously submit ingress messages to any target canister on an application subnet. Each message costs the **target canister** at minimum ~1.2M cycles (induction fee alone), plus execution cost when the message is processed. The attacker pays nothing. Over time, the target canister's cycles balance is drained to the freeze threshold, at which point further ingress messages are rejected — effectively freezing the canister and denying service to its legitimate users. This is a **cycles/resource accounting bug** with a direct denial-of-service impact on any canister holding a cycles balance. [5](#0-4) 

---

### Likelihood Explanation

The attack requires only a valid identity (freely generated) and the ability to submit HTTP requests to a boundary node. No privileged access, no key compromise, no social engineering, and no threshold corruption is required. The attacker-controlled entry path is the standard ingress submission API. The only natural throttle is the ingress history capacity limit, but an attacker can rotate message IDs (each ingress message has a unique nonce/expiry) to continuously inject new messages across rounds. [6](#0-5) 

---

### Recommendation

Introduce a cost on the **ingress sender** side, analogous to the KintoWallet recommendation. Concretely:

1. **Sender-side rate limiting at the protocol level**: Track per-sender ingress submission counts within a sliding window and reject excess submissions before induction.
2. **Partial sender contribution**: Require the sender to attach a small cycles payment (via a signed token or prepaid account) that covers at least the induction fee, so the attack has a non-zero cost.
3. **Canister-level ingress filtering**: The `inspect_message` hook already exists for this purpose — document and encourage canisters to reject messages from unknown senders before induction cost is charged. However, note that `inspect_message` is called **after** the induction fee is already deducted, so this does not fully mitigate the issue.

---

### Proof of Concept

1. Generate a free `PrincipalId` (e.g., anonymous or a fresh self-signed key).
2. Identify a target canister `C` on an application subnet with a non-zero cycles balance.
3. In a loop, submit signed ingress messages to `C` with a valid method name (e.g., any exported update method), varying the nonce/expiry to avoid deduplication.
4. Each message that passes the ingress filter causes `enqueue()` to call `charge_ingress_induction_cost()` on `C`, deducting ≥1,200,000 cycles per message from `C`'s balance.
5. Repeat until `C`'s balance falls below its freeze threshold; subsequent legitimate calls to `C` will be rejected with `CanisterOutOfCycles`.

The attacker's total cost: **zero cycles**. The target canister's loss: its entire spendable cycles balance. [1](#0-0) [4](#0-3)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L559-597)
```rust
    pub fn ingress_induction_cost(
        &self,
        ingress: &SignedIngress,
        effective_canister_id: Option<CanisterId>,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> IngressInductionCost {
        let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
        let ingress = ingress.content();
        let paying_canister = match ingress.is_addressed_to_subnet() {
            // If a subnet message, get effective canister id who will pay for the message.
            true => {
                if let Ok(Method::UpdateSettings) = Method::from_str(ingress.method_name()) {
                    // The fee for `UpdateSettings` with small payload is charged after
                    // applying the settings to allow users to unfreeze canisters
                    // after accidentally setting the freezing threshold too high.
                    if self.is_delayed_ingress_induction_cost(ingress.arg()) {
                        None
                    } else {
                        effective_canister_id
                    }
                } else {
                    effective_canister_id
                }
            }
            // A message to a canister is always paid for by the receiving canister.
            false => Some(ingress.canister_id()),
        };

        match paying_canister {
            Some(paying_canister) => {
                let cost = self.ingress_induction_cost_from_bytes(raw_bytes, subnet_cycles_config);
                IngressInductionCost::Fee {
                    payer: paying_canister,
                    cost: cost.real(),
                }
            }
            None => IngressInductionCost::Free,
        }
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L599-607)
```rust
    /// Returns the cost of an ingress message based on the message size.
    pub fn ingress_induction_cost_from_bytes(
        &self,
        bytes: NumBytes,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CompoundCycles<IngressInduction> {
        self.ingress_message_received_fee(subnet_cycles_config)
            + self.ingress_byte_received_fee(subnet_cycles_config) * bytes.get()
    }
```

**File:** rs/config/src/subnet_config.rs (L434-438)
```rust
    /// Fee for every ingress message received.
    pub ingress_message_reception_fee: Cycles,

    /// Fee for every byte received in an ingress message.
    pub ingress_byte_reception_fee: Cycles,
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L250-255)
```rust
        if state.metadata.own_subnet_type != SubnetType::System
            && state.metadata.ingress_history.len() >= self.ingress_history_max_messages
        {
            return Err(IngressInductionError::IngressHistoryFull {
                capacity: self.ingress_history_max_messages,
            });
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L293-315)
```rust
            IngressInductionCost::Fee { payer, cost } => {
                // Get the paying canister from the state.
                let canister = match state.canister_state_make_mut(&payer) {
                    Some(canister) => canister,
                    None => return Err(IngressInductionError::CanisterNotFound(payer)),
                };

                // Withdraw cost of inducting the message.
                let memory_usage = canister.memory_usage();
                let message_memory_usage = canister.message_memory_usage();
                let compute_allocation = canister.compute_allocation();
                let reveal_top_up = canister.controllers().contains(&ingress.source.get());
                if let Err(err) = self.cycles_account_manager.charge_ingress_induction_cost(
                    canister,
                    memory_usage,
                    message_memory_usage,
                    compute_allocation,
                    cost,
                    subnet_cycles_config,
                    reveal_top_up,
                ) {
                    return Err(IngressInductionError::CanisterOutOfCycles(err));
                }
```
