### Title
Unprivileged Third-Party Can Permanently Destroy Victim Neuron's Age Bonus via `refresh_neuron` — (`rs/nns/governance/src/governance.rs`)

---

### Summary

An unprivileged attacker can permanently reduce any NNS neuron's age (and thus its age bonus, up to 25% voting power multiplier) to near-zero by depositing ICP to the victim's subaccount and then calling `manage_neuron` with `ClaimOrRefresh { by: By::NeuronIdOrSubaccount }`. No authorization check exists on this code path.

---

### Finding Description

**Step 1 — No authorization on `By::NeuronIdOrSubaccount` refresh.**

In `manage_neuron_internal`, the `ClaimOrRefresh` command is handled before any neuron ownership check. The comment at line 6104 even explains why: the neuron may not exist yet (for claims). For the `By::NeuronIdOrSubaccount` variant, the code directly calls `refresh_neuron_by_id_or_subaccount` with no `caller` validation: [1](#0-0) 

`refresh_neuron` itself also performs no ownership check — it only locks the neuron and queries the ledger balance: [2](#0-1) 

**Step 2 — Age dilution math in `update_stake_adjust_age`.**

When the ledger balance exceeds the cached stake, `update_stake_adjust_age` is called with the new total balance. It computes the new age via `combine_aged_stakes(S, A, D, 0)` — treating the added amount `D` as having age 0: [3](#0-2) 

The formula in `combine_aged_stakes` is:

```
new_age = (S * A + D * 0) / (S + D) = S * A / (S + D)
``` [4](#0-3) 

If `D >> S`, `new_age → 0`. A victim neuron with 8-year-old stake `S` can have its age reduced to near-zero by depositing `D = 1000 * S` ICP.

**Step 3 — ICP ledger is permissionless.**

Any principal can transfer ICP to any account identifier, including a governance subaccount belonging to another user's neuron. No permission is required.

---

### Impact Explanation

- The NNS age bonus multiplier scales linearly from 0% to 25% over 4 years (max at 8 years).
- A neuron with 8 years of age has a 25% voting power bonus. After the attack with `D = 1000 * S`, the age becomes `S * A / (S + 1000*S) ≈ A / 1001 ≈ 0`.
- The age is **permanently destroyed** — it cannot be recovered without waiting another 8 years.
- This reduces the victim's voting power and staking rewards without their consent.
- The attacker's cost is the ICP deposited (which is locked in the victim's neuron, not lost — but the attacker cannot retrieve it). For a victim with `S = 1 ICP`, depositing `D = 1000 ICP` costs the attacker 1000 ICP to destroy the age bonus. For large neurons, the cost scales proportionally.

---

### Likelihood Explanation

- The attack requires no privileged access, no key compromise, and no governance majority.
- The entry point is a standard `manage_neuron` ingress call, callable by any principal.
- The ICP transfer is a standard ledger operation.
- The attack is deterministic and locally testable.
- The only cost to the attacker is the deposited ICP (which becomes permanently locked in the victim's neuron, making it a griefing/vandalism attack rather than a theft).

---

### Recommendation

Add a caller authorization check in the `By::NeuronIdOrSubaccount` refresh path, requiring the caller to be the neuron's controller or a registered hotkey. Alternatively, disallow `refresh_neuron` from reducing the `aging_since_timestamp_seconds` unless the caller is authorized — i.e., only allow age adjustment when the neuron owner explicitly adds stake.

---

### Proof of Concept

```
Victim neuron: NotDissolving, S = 100_000_000 e8s (1 ICP), A = 252_288_000 (8 years in seconds)

Attacker:
1. Transfers D = 100_000_000_000 e8s (1000 ICP) to victim's governance subaccount via ICP ledger.
2. Calls governance.manage_neuron({
       neuron_id_or_subaccount: NeuronId(victim_id),
       command: ClaimOrRefresh { by: NeuronIdOrSubaccount({}) }
   }) from any principal.

Result:
  new_age = (100_000_000 * 252_288_000) / (100_000_000 + 100_000_000_000)
           = 25_228_800_000_000_000 / 100_100_000_000
           ≈ 252_035 seconds  (~2.9 days, down from 8 years)

Age bonus: reduced from ~25% to ~0.2%.
Voting power multiplier: destroyed.
```

The `combine_aged_stakes` proptest in the codebase already confirms the weighted-average invariant holds — the combined age is always between the two input ages (0 and A), confirming the dilution is by design for the owner's own stake additions, but is exploitable by third parties: [5](#0-4)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5900-5961)
```rust
    async fn refresh_neuron(
        &mut self,
        nid: NeuronId,
        subaccount: Subaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let account = neuron_subaccount(subaccount);
        // We need to lock the neuron to make sure it doesn't undergo
        // concurrent changes while we're checking the balance and
        // refreshing the stake.
        let now = self.env.now();
        let _neuron_lock = self.lock_neuron_for_command(
            nid.id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(InFlightCommand::ClaimOrRefreshNeuron(
                    claim_or_refresh.clone(),
                )),
            },
        )?;

        // Get the balance of the neuron from the ledger canister.
        tla_log_locals! { neuron_id: nid.id };
        let balance = self.ledger.account_balance(account).await?;
        let min_stake = self.economics().neuron_minimum_stake_e8s;
        if balance.get_e8s() < min_stake {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to refresh a neuron. \
                     Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }
        self.with_neuron_mut(&nid, |neuron| {
            match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
                Ordering::Greater => {
                    println!(
                        "{}ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                        LOG_PREFIX,
                        account,
                        balance.get_e8s(),
                        neuron.cached_neuron_stake_e8s
                    );
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                Ordering::Less => {
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                // If the stake is the same as the account balance,
                // just return the neuron id (this way this method
                // also serves the purpose of allowing to discover the
                // neuron id based on the memo and the controller).
                Ordering::Equal => (),
            };
        })?;

        Ok(nid)
```

**File:** rs/nns/governance/src/governance.rs (L6104-6141)
```rust
        // We run claim or refresh before we check whether a neuron exists because it
        // may not in the case of the neuron being claimed
        if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
            // Note that we return here, so none of the rest of this method is executed
            // in this case.
            return match &claim_or_refresh.by {
                Some(By::Memo(memo)) => {
                    let memo_and_controller = MemoAndController {
                        memo: *memo,
                        controller: None,
                    };
                    self.claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller,
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
                }
                Some(By::MemoAndController(memo_and_controller)) => self
                    .claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller.clone(),
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response),

                Some(By::NeuronIdOrSubaccount(_)) => {
                    let id = mgmt.get_neuron_id_or_subaccount()?.ok_or_else(|| {
                        GovernanceError::new_with_message(
                            ErrorType::NotFound,
                            "No neuron ID specified in the management request.",
                        )
                    })?;
                    self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                        .await
                        .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
```

**File:** rs/nns/governance/src/neuron/types.rs (L1021-1026)
```rust
            let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
                self.cached_neuron_stake_e8s,
                self.age_seconds(now),
                updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
                0,
            );
```

**File:** rs/nns/governance/src/neuron/mod.rs (L31-34)
```rust
        let total_age_seconds: u128 = ((x_stake_e8s as u128)
            .saturating_mul(x_age_seconds as u128)
            .saturating_add((y_stake_e8s as u128).saturating_mul(y_age_seconds as u128)))
            / ((x_stake_e8s as u128).saturating_add(y_stake_e8s as u128));
```

**File:** rs/nns/governance/src/neuron/mod.rs (L95-111)
```rust
    proptest! {
        #[test]
        fn test_combine_aged_stakes_invariant(
            x_stake_e8s in 0..10_000_000_000 * E8, // Choosing u64::MAX can cause overflow for the combined stake
            x_age_seconds in 0..u64::MAX,
            y_stake_e8s in 0..10_000_000_000 * E8,
            y_age_seconds in 0..u64::MAX,
        ) {
            let (stake_e8s, age_seconds) = combine_aged_stakes(x_stake_e8s, x_age_seconds, y_stake_e8s, y_age_seconds);
            prop_assert_eq!(stake_e8s, x_stake_e8s + y_stake_e8s);

            // The combined age should be between the two input ages.
            let is_combined_age_between_input_ages = (y_age_seconds <= age_seconds && age_seconds <= x_age_seconds) ||
               (x_age_seconds <= age_seconds && age_seconds <= y_age_seconds);
            let are_both_stakes_zero = x_stake_e8s == 0 && y_stake_e8s == 0;
            prop_assert!(are_both_stakes_zero || is_combined_age_between_input_ages);
        }
```
