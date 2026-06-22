### Title
Predictable PRNG Seeded Solely from Publicly Observable IC Consensus Time in SNS Governance Canister - (File: `rs/sns/governance/canister/canister.rs`)

---

### Summary

The SNS governance canister's production `CanisterEnv` seeds its `ChaCha20Rng` PRNG exclusively with `ic_cdk::api::time()` (the IC consensus timestamp in nanoseconds) at canister initialization. The IC consensus time is deterministic, identical across all replicas, and publicly observable on-chain. The seed construction also copies the same 128-bit value into both halves of the 256-bit seed, halving the effective entropy. All subsequent outputs of `insecure_random_u64()` — used as ledger transaction memos in `merge_maturity` — are therefore fully predictable by any external observer.

---

### Finding Description

In `rs/sns/governance/canister/canister.rs`, `CanisterEnv::new()` constructs the PRNG seed as follows:

```rust
let now_nanos = now_nanoseconds() as u128;
let mut seed = [0_u8; 32];
seed[..16].copy_from_slice(&now_nanos.to_be_bytes());
seed[16..32].copy_from_slice(&now_nanos.to_be_bytes());
ChaCha20Rng::from_seed(seed)
``` [1](#0-0) 

`now_nanoseconds()` resolves to `ic_cdk::api::time()` on wasm32, which returns the IC consensus time — a value that is:

1. **Publicly observable**: The block time is visible to any observer of the IC state.
2. **Deterministic across all replicas**: By protocol design, all replicas agree on the same timestamp.
3. **Low-entropy**: At most ~64 bits of entropy (nanosecond timestamp), and both halves of the 32-byte seed are identical copies of the same value, making the effective seed space even smaller. [2](#0-1) 

The code comment claims "the resulting number isn't easily predictable from the outside," which is factually incorrect for the IC context. [3](#0-2) 

The `insecure_random_u64()` method, which draws from this seeded PRNG, is called in production code in `merge_maturity()` to generate the memo/nonce for a minting ledger transfer:

```rust
self.env.insecure_random_u64(), // Random memo(nonce) for the ledger's transaction
``` [4](#0-3) 

The `Environment` trait definition confirms `insecure_random_u64` is the production interface: [5](#0-4) 

---

### Impact Explanation

An attacker who observes the SNS governance canister's initialization block (publicly available on-chain) can:

1. Reconstruct the exact PRNG seed (`now_nanos` duplicated into both halves of the 32-byte seed).
2. Replay the ChaCha20 stream to predict every future output of `insecure_random_u64()`.
3. Know in advance the memo value that will be used in any future `merge_maturity` minting transfer.

The predictable memo enables a **deduplication-based DoS** on `merge_matistry`: if the ICRC-1 ledger's deduplication window is active and `created_at_time` is set in the transfer, an attacker who pre-submits a transaction with the predicted memo from the same account can cause the governance canister's `merge_maturity` call to be rejected as a duplicate, permanently blocking that neuron's maturity merge until the PRNG advances past the collision. Additionally, after any canister upgrade, `CanisterEnv::new()` is called again, re-seeding the PRNG from the new upgrade time — creating a fresh predictable sequence and potentially re-colliding with prior memos if the upgrade time is close to the original initialization time.

The `mint_tokens` path at line 6531 also uses `insecure_random_u64()` but is gated behind `check_test_features_enabled()`, limiting its production exposure. [6](#0-5) 

---

### Likelihood Explanation

- The canister initialization time is recorded in the IC state and is trivially observable by any user querying the IC.
- The number of prior `insecure_random_u64()` calls can be estimated from on-chain `merge_maturity` transaction history.
- No privileged access, key compromise, or subnet-majority corruption is required.
- The attacker entry path is an unprivileged ingress call to the SNS ledger canister with a crafted memo.

---

### Recommendation

Replace the time-seeded PRNG with a seed derived from `raw_rand` (the IC management canister's cryptographically secure randomness API), called once at initialization and periodically refreshed. The NNS governance canister already implements this pattern correctly via its `SeedingTask`: [7](#0-6) 

Adopt the same approach for SNS governance: call `IC_00.raw_rand()` asynchronously at `canister_init` and `canister_post_upgrade`, store the resulting seed, and re-seed the PRNG on a periodic timer (as NNS governance does with `SEEDING_INTERVAL`). Until then, do not rely on `insecure_random_u64()` for any value that must be unpredictable to external observers.

---

### Proof of Concept

```
1. Deploy SNS governance canister at IC consensus time T (publicly observable).
2. Construct seed: seed[0..16] = T.to_be_bytes(); seed[16..32] = T.to_be_bytes()
3. Instantiate ChaCha20Rng::from_seed(seed).
4. Count N = number of merge_maturity calls made since initialization (from ledger history).
5. Advance the RNG N times: for _ in 0..N { rng.next_u64(); }
6. The next rng.next_u64() is the memo that the next merge_maturity call will use.
7. Submit an ICRC-1 transfer to the SNS ledger from any account with that memo and
   created_at_time set, targeting the governance minting account.
8. When the neuron controller calls merge_maturity, the governance canister's
   transfer_funds call is rejected by the ledger as a duplicate transaction,
   causing merge_maturity to return an error and the neuron's maturity merge to fail.
```

### Citations

**File:** rs/sns/governance/canister/canister.rs (L102-111)
```rust
            // Seed the pseudo-random number generator (PRNG) with the current time.
            //
            // All replicas are guaranteed to see the same result of now() and the resulting
            // number isn't easily predictable from the outside.
            //
            // Why we don't use raw_rand from the ic00 api instead: this is an asynchronous
            // call so can't really be used to generate random numbers for most cases.
            // It could be used to seed the PRNG, but that wouldn't add any security regarding
            // unpredictability since the pseudo-random numbers could still be predicted after
            // inception.
```

**File:** rs/sns/governance/canister/canister.rs (L112-118)
```rust
            rng: {
                let now_nanos = now_nanoseconds() as u128;
                let mut seed = [0_u8; 32];
                seed[..16].copy_from_slice(&now_nanos.to_be_bytes());
                seed[16..32].copy_from_slice(&now_nanos.to_be_bytes());
                ChaCha20Rng::from_seed(seed)
            },
```

**File:** rs/sns/governance/canister/canister.rs (L190-201)
```rust
fn now_nanoseconds() -> u64 {
    if cfg!(target_arch = "wasm32") {
        ic_cdk::api::time()
    } else {
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .expect("Failed to get time since epoch")
            .as_nanos()
            .try_into()
            .expect("Failed to convert time to u64")
    }
}
```

**File:** rs/sns/governance/src/governance.rs (L1499-1507)
```rust
        let _block_height: u64 = self
            .ledger
            .transfer_funds(
                maturity_to_merge,
                0, // Minting transfer don't pay a fee
                None, // This is a minting transfer, no 'from' account is needed
                self.neuron_account_id(subaccount), // The account of the neuron on the ledger
                self.env.insecure_random_u64(), // Random memo(nonce) for the ledger's transaction
            )
```

**File:** rs/sns/governance/src/governance.rs (L6515-6535)
```rust
    pub async fn mint_tokens(
        &mut self,
        mint_tokens_request: MintTokensRequest,
    ) -> MintTokensResponse {
        self.check_test_features_enabled();

        self.ledger
            .transfer_funds(
                mint_tokens_request.amount_e8s(),
                0,    // Minting transfer don't pay a fee
                None, // This is a minting transfer, no 'from' account is needed
                mint_tokens_request
                    .recipient
                    .expect("recipient must be set")
                    .try_into()
                    .unwrap(), // The account of the neuron on the ledger
                self.env.insecure_random_u64(), // Random memo(nonce) for the ledger's transaction
            )
            .await
            .unwrap();
        MintTokensResponse {}
```

**File:** rs/sns/governance/src/types.rs (L2100-2103)
```rust
    /// Returns a random number.
    ///
    /// This number is the same in all replicas.
    fn insecure_random_u64(&mut self) -> u64;
```

**File:** rs/nns/governance/src/timer_tasks/seeding.rs (L31-50)
```rust
        let result: Result<Vec<u8>, (Option<i32>, String)> = env
            .call_canister_method(IC_00, "raw_rand", Encode!().unwrap())
            .await;

        let next_delay = match result {
            Ok(bytes) => {
                let seed = Decode!(&bytes, [u8; 32]).unwrap();
                self.governance.with_borrow_mut(|governance| {
                    governance.seed_rng(seed);
                });
                SEEDING_INTERVAL
            }
            Err((code, msg)) => {
                println!(
                    "{}Error seeding RNG. Error Code: {:?}. Error Message: {}",
                    LOG_PREFIX, code, msg
                );
                RETRY_SEEDING_INTERVAL
            }
        };
```
